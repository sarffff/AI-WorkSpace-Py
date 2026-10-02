# 生产化缺口与已知问题清单

> 范围：本轮补齐 P0（取消 / 模型 Fallback / Office 解析 / URL 入库 / 索引重启恢复 / 能力档案）**之后**仍未覆盖的部分。
> 分三类：**A. 已实现功能的边界（技术债）**、**B. 未实现的功能缺口（P1/P2）**、**C. 探索中发现的具体隐患**。
> 判据沿用 `standard.md` 的"生产级 Agent 十二条能力"。

---

## A. 本轮 P0 已实现、但有明确未覆盖情形（技术债）

这些不是 bug，是**刻意收窄的边界**，代码注释与 `.env.example` 里都写明了。列出来是为了下一步知道从哪接。

| 编号 | 项 | 已做到 | 未覆盖 | 影响 |
|---|---|---|---|---|
| A1 | ~~取消/中断（P0-1）~~ ✅ | 单进程内 `asyncio.Event`（同 worker 快路径）**+ 轮首顺带查落库的 `agent_runs.status==cancelled`**（`checkpoint_store.is_run_cancelled`）——取消请求打到别的 worker 时，那边 `mark_cancelled` 落的终态本进程循环也看得到 | 工具前的安全点仍只查进程内 Event（避免每工具一次 DB 读）；跨 worker 的叫停最晚在下一轮轮首生效，不是立刻 | 多 worker 下「停止生成」真的能停 |
| A2 | 主模型 Fallback（P0-2） | 同 endpoint 换模型名，开流前切换，成本按实际模型记账 | **跨提供商故障切换**：整个提供商宕机（base_url/key 级）时无备用；需 per-provider 凭证列表 | 提供商整体不可用 = 主回答仍报错 |
| A3 | Office 解析（P0-4a） | pdf / docx / xlsx / pptx 结构化解析入库；**扫描件 PDF OCR 已补**（视觉模型逐页转写，默认关，`INGEST_OCR_*`，见 `services/ocr.py`） | **图片型 docx/pptx 的内嵌图 OCR**：常是 logo 而非内容页、质量存疑，本轮未做，仍落 `no_extractable_text` | 扫描件 PDF 配了视觉模型即可入库；图片型 Office 文档仍进不了库 |
| A4 | ~~索引重启恢复（P0-3）~~ ✅ 持久队列 | 启动重跑卡在 `processing` 的文档；**持久任务队列已补**：`document_jobs` 表 + 认领/租约/重试/进度 + 过期租约 reaper（`services/document_queue.py`，照搬 agent_runs 的 lease/reaper），上传与启动重驱都改走它 | **Qdrant 未转默认**（刻意：保留零依赖 memory 默认，多 worker 时显式切；设 `WEB_CONCURRENCY>1` 却仍 memory 时启动告警）；进度仍是粗粒度（queued/running/done） | 多 worker / 批量导入已有背压与重试；横向扩仍需显式切 Qdrant |
| A5 | 能力档案（P0-5） | `/settings` 集中只读上报 + 设置页展示 | **运行时切换**：全是进程级 `.env` 配置，改完要重启；没有"配置档案一键切换 + 校验"的引导流程 | 运营方拼一个可用 Agent 仍需读懂 `.env.example` 并手工配对提示词版本 |

---

## B. 未实现的功能缺口

### P1 — 影响体验与可持续运营

| 编号 | 缺口 | 现状 | 说明 |
|---|---|---|---|
| B1 | **通知系统（主动外推）** | 站内收件箱（per-user 持久）+ 顶栏铃铛 / 未读红点 / 下拉收件箱已上线；审批挂起、待回答、超时废弃、线上健康告警都会落一条通知，点带 chatId 的通知跳回对应会话去处置 | 仍缺**主动外推**（IM / 邮件 / WebSocket）：审批人不打开应用时收不到。站内闭环已接上，离场推送属外部基建，留作后续 |
| B2 | **多租户 / 协作 / RBAC** | 单工作区 + `admin`/`user` 两级 | 无细粒度角色（编辑者/审批人/只读审计员分离）、无租户级配额（`usage_guard` 只到用户级）、Agent 无独立身份与短期凭证（直接用发起人权限） |
| B3 | ~~**评估线上回流 + 告警**~~ ✅ | 离线金标强（54题RAG/29任务Agent+门禁）+ 线上差评→回归用例；**主动告警**（`services/production_monitor.py`，默认关）；**线上抽样送评**（`eval/online_sample.py`：真实问答重检索后送 AnswerJudge，低分导成回归候选）；**多次运行看方差**（`eval.run --repeat N --temperature 0.7`，报告出 cv/stdev 小节） | 本轮补齐。遗留：抽样重检索的 context 是当前库而非当初那条回答真用的（faithfulness 会漂，relevance 准），已写进模块文档 |
| B4 | ~~**会话回放 UI**~~ ✅ 只读 | 只读回放时间线已上线：检查点目录渲染成时间线，点一格看那一轮当时的 messages / 待调工具 / 计划 / 预算余额 / 熔断（`widgets/run-replay`；轨迹页 `?run=` 整页切换 + 对话里每条回答的「回放执行」入口，runId 从该回答的工具轨迹取） | **「回到第 N 轮重跑」仍无**（刻意：重跑要 fork 新 run 并处理副作用工具被重放的风险，是另一件事） |
| B5 | ~~**回答级操作**~~ ✅（分叉除外） | regenerate（重新生成）、编辑后重发、引用点击跳原文（`widgets/citation-source` 来源查看器，命中块高亮 + 邻域）、对话导出（会话列表悬浮按钮，Markdown）均已接 | **分支对话**仍缺（需 `parent_message_id` 消息树；最重，留作后续） |

### P2 — 能力天花板（可后置）

| 编号 | 缺口 | 现状 |
|---|---|---|
| B6 | **MCP 支持** | 完全没有。工具生态扩展目前只能改代码注册（`standard.md` 称 MCP 为事实标准） |
| B7 | **代码执行沙箱** | 无 shell/代码解释器工具，只有 fs 读写（`list_directory`/`read_file`/`write_file`）。数据分析类任务做不了 |
| B8 | **Diff 式产出审查** | `write_file` 是整文件写入，无逐块接受/拒绝的审查界面 |
| B9 | **Plan-then-Execute 的 HITL** | 规划已实现，但计划只是"注入的指引"，无"执行前展示计划、允许用户编辑"环节 |
| B10 | **多代理委派** | 已实现且评估过，但默认关闭：`augment` 多花 29% 输入 token 换不到成功率、`supervisor` 有已定位的轮次预算缺陷（见 `eval/reports/agent-eval-*.md`） |
| B11 | **浏览器自动化 / Computer Use** | 无 |
| B12 | **Docker 交付** | README 部署章节仍是 `TODO: 添加 Dockerfile`（属部署，按需求略过） |

---

## C. 探索中发现的具体隐患（小、但真实）

| 编号 | 位置 | 问题 | 严重度 |
|---|---|---|---|
| C1 | `back-end/services/agent_state.py` `RunStatus` | Literal 列了 running/waiting_approval/waiting_input/done/failed/abandoned/cancelled，但 `checkpoint_store.mark_interrupted` 与 `reap_orphan_runs` 实际会写入/查询 **`"interrupted"`**，该值不在 Literal 里。运行时没问题（DB 列是 `String(24)`），但**后端类型定义与实际取值不一致**（前端 `AgentRunDetail.status` 已手工补全 interrupted，后端 Literal 仍缺） | 低（类型一致性） |
| C2 | `chat_service` 落 assistant 消息 | 主模型 fallback 到备用模型时，span 里有 `fallback_model`、成本按实际模型记账，但 `messages.model` 列仍记**请求的主模型名**。归因上"这条回答是哪个模型答的"会对不上 | 低（可观测性归因） |
| C3 | `README.md` | 测试数字过时：写"后端(1041 条)""前端(47 条)"，实际前端 vitest 现为 140 条、后端本轮也新增了约 45 条 | 极低（文档） |
| C4 | 安全默认值 | `JWT_SECRET_KEY` 有默认占位值、Qdrant 默认无鉴权（`QDRANT_API_KEY` 空）。私有化交付前需一轮安全默认值收口 | 中（交付前必查） |

---

## 建议补齐顺序

1. **B1 通知**（把 HITL 闭环接上，审批人不在场时最致命）
2. **A2 跨提供商 Fallback + C4 安全默认值**（可用性 + 交付前收口）
3. ~~**A3 OCR**~~ —— 扫描件 PDF 已补（视觉模型逐页转写，默认关）；图片型 docx/pptx 的内嵌图 OCR 仍开着
4. ~~**B3 线上抽样评估 + 告警**~~ ✅ 已补齐（production_monitor 告警 + online_sample 抽样回流 + run --repeat 方差）
5. ~~**B4 回放 UI + B5 回答级操作 + 通知中心 UI**~~ ✅（分叉对话除外）—— 后端端点已补：回放只读快照 `GET /chats/runs/{id}/checkpoints/{seq}`、引用跳原文 `GET /knowledge/documents/{id}/chunks/{idx}`、对话导出 `GET /chats/{id}/export`。**前端已接**：通知中心（顶栏铃铛 + 未读红点 + 下拉收件箱，轮询 `/notifications/unread_count` 兼做 B3 健康心跳，补 B1「后端上线、前端零消费」那半边）；只读回放时间线（轨迹页 `?run=` 整页切换 + 对话每条回答的「回放执行」入口，`widgets/run-replay`）；引用点击跳原文（`widgets/citation-source` 来源查看器，命中块高亮 + 邻域）；对话导出按钮（会话列表悬浮操作，Markdown）。regenerate/编辑重发本就有。**仍缺：分叉对话**（最重，需 `parent_message_id` 消息树；现 `revise_user_message` 是删尾巴、恰好相反）
6. **A4 持久队列 + A1 跨 worker 取消** ✅ 后端已补 —— 持久 `document_jobs` 队列（认领/租约/重试/reaper）+ 轮首查落库 `cancelled`；**Qdrant 保留 memory 默认**（不翻转，多 worker 显式切 + 启动告警）

> A1（跨 worker 取消）与 A4（多 worker 前提）是同一个约束：本轮一起补了后端——取消改为轮首兼查落库终态、入库改走持久队列。向量库默认仍是 memory（零依赖），多 worker 横向扩时显式切 Qdrant，`WEB_CONCURRENCY>1` 却仍 memory 时启动会告警。

---

## D. 2026-10-02 代码审计：本轮已修 / 明确不做

这一节和上面三节的来源不同：上面是"实现时知道的边界"，这里是**拿代码逐条对出来的正确性缺口**（四个分析代理并行审计 + 我亲自复跑关键几条）。表里区分两种可信度：

- **✅实测** = 我自己在真 MySQL / 真测试跑出来的，可直接当事实用。
- **⏳未复核** = 代理给了 `文件:行号`，我没重跑。**动手前先自己验一遍**，别当结论传播。

### D1 本轮已修（后端 1706 条、前端 149 条 + tsc 全绿）

| 项 | 位置 | 修了什么 |
|---|---|---|
| ✅实测 **审批可执行两遍** | `services/checkpoint_store.py` `claim_for_resume` + `chat_service.py` 三个入口 | 原来是"SELECT 读 status → 判断 → 无条件 UPDATE"，两个进程各读到一次 `waiting_approval` 就都放行。改成短事务条件 UPDATE 抢占（**不进 LLM 流**，见下方取舍）。回归 `tests/test_resume_claim.py` 8 条 |
| ✅实测 **`answer_clarification` 的状态翻转从来没生效** | `checkpoint_store.enabled()` | 它读的是自己模块的 settings 绑定，运行时赋值与 monkeypatch 都碰不到。后果是澄清后的 run 停在 `waiting_input`，而 `reap_orphan_runs` 只扫 `running` → **永远进不了孤儿回收**。测试从没暴露（没人用 1205/超时去试它） |
| ✅实测 **离开对话页停不掉流** | `front-end/src/pages/chat/ui/ChatPage.tsx` | 卸载路径原先什么都不做（只有停止按钮 abort）。切页再回来 `abortRef`/`activeRunRef` 为 null，服务端那一跑继续烧 token、旧生成器继续往气泡长字，且再没有入口能停。抽出 `stopLocalStream`，卸载用 latest-ref 调用；**只拆本地流、不 cancelRun**——断线由服务端 finally 标 `interrupted`，才能被下面那条接回去 |
| ✅实测 **`getResumableRuns` 零调用方** | `front-end/src/widgets/resumable-runs` | 后端 `/chats/runs/resumable` 与 client 方法早就有，界面上没有入口 → 刷新后 `interrupted` 那轮等于不存在，只能重问（前几轮检索全丢）。新增提示条 + 5 条测试 |
| ✅实测 **写工具与审批的默认值不对称** | `services/security_preflight.py` `audit_write_gates` | 两个审批开关默认都关，而 `TOOL_*_ENABLED` 是一个个开的：只开写工具=能写但没人确认。新增启动闸门（生产拒绝启动 / dev 告警），输入取自 `enabled_names()`/`gated_tools()` 同一份代码，另加一条防腐断言。**注意：现有 `.env` 是配对正确的（mode=write + 快照开），这条闸门拦的是下一个 clone 的人** |
| ✅实测 **models 与库 7 项漂移 → 1 项** | `models.py` | 索引名跟着迁移走（`ix_message_tool_steps_run`）、去掉两处重复/冗余 `index=True`、删掉 `users/documents.workspace_id` 上那条库里从未存在且**永不兑现**的 `ondelete=SET NULL`（应用层从不删工作区）。全部模型侧，没动生产数据 |
| ✅实测 **schema 版本漂移只在 Electron 启动器可查** | `run.js` + `back-end/scripts/check_schema_drift.py` | `init_db()` 见到 `alembic_version` 就不再建表，所以"库落后于代码"能安静启动成功、只在碰新表时 1146。现在起后端前会判 `up-to-date/behind/unmanaged/branch/unknown-revision/unreachable` 并询问是否 upgrade；`--no-migrate` 只报告。**仍绕过 `uvicorn main:app` 直启**（要堵死得在 `main.py` startup 里查） |

**审批那一条的取舍**（决定没写成"锁住整个流"）：`SELECT ... FOR UPDATE` 的锁属于事务而非语句，而 resume 是流式生成器（一轮 5–10 秒）——同 run 的取消请求（`cancellation.register` 要 INSERT `agent_runs` 子行，InnoDB 会先 S-锁父行）、SSE 断线的 `mark_interrupted`、审计追加全得排队，MySQL 还会把行锁扩成表锁（1205 而非 1200）。所以抢占只做一次 UPDATE + 立即 commit，干活在锁外。顺带一个真缺陷被这条路带出来：`continue_orphan` 允许从 `running` 接回，而 `running→running` 谓词值没变、条件 UPDATE 对它无效，所以那一路改判"多久没人碰过"（`updated_at < stale_before`），并发接续因此自动只留一个赢家。

### D2 迁移链：明确没做，以及为什么

| 事实 | 证据 |
|---|---|
| ✅实测 `alembic upgrade head` **从空库跑不起来** | `0001_baseline.py:44` 用 `Base.metadata.create_all`——建的是**今天的** models（它的注释还写着"从下一条开始必须是历史快照"），于是 0006/0007 的 `add_column` 必然撞 duplicate column。SQLite 上实测停在 `0003_message_feedback`（缺 `batch_alter_table`，`0010` 同） |
| ✅实测 CI 完全不碰 alembic | `.github/workflows/` grep `alembic\|mysql\|3306` 零命中；`tests/conftest.py:399` 唯一真库 fixture 是 SQLite `create_all`。所以**整条迁移链从未被任何测试执行过** |
| ✅实测 `alembic check` 现在仍红 1 项 | 只剩 `messages.seq`：模型是 `autoincrement=True` 的非主键列，MySQL 要求 AUTO_INCREMENT 必须 NOT NULL，而 SQLite 上 NOT NULL 会让所有测试的消息插入炸。这就是 `database.py:74` 那句硬编码 ALTER 的来路。真要收，得把 seq 改成应用层赋值（照 `audit_log.py:84` 的 max+1），属改写入路径 |

**为什么没顺手做**：要加"改了 models 忘写迁移"的 CI 门禁，唯一可行的判据是"从空库跑完整链再对账"——按生产的 `create_all`+`stamp` 路径对账是**恒绿、零信息量**的。而跑通整链要把 `0001_baseline` 重写成冻结的显式建表（迁移历史手术）。现有 stamp 库永远不会重跑 0001，所以手术对已有数据零影响，但那是另一个量级的工作，留待单独决定。

### D3 待办：多 worker（用户明确"先记着"）

以下全部 ⏳未复核，动手前先自己验。共同形状：**进程内状态在 `WEB_CONCURRENCY>1` 下被稀释成 N 份**。

| 位置 | 问题 | 触发条件 |
|---|---|---|
| `services/semantic_cache.py:114` | 缓存桶与 `invalidate_*` 全在进程内 `_buckets`。删文档只清本进程 → 别的 worker 仍能命中引用该文档的旧答案。这正是 `drop()` 注释声称要防的事 | 多 worker + 删除/撤共享 |
| `services/usage_guard.py:171` | 频率计数进程内，注释写的理由"项目现在没有 Redis 依赖"已过期（`redis_service.py` 在用） | `WEB_CONCURRENCY=N` → 上限变 N 倍。成本/token 两道读 `trace_spans`，无此问题 |
| `rate_limit.py:5` | `Limiter` 未传 `storage_uri` → slowapi 内存存储，登录 `5/minute` 按 worker 各算 | 多 worker |
| `services/document_queue.py:83` | 租约 600s，但 OCR 最坏 `20 页 × 60s = 1200s` → 合法运行必然超租约，被 reaper 重投入队，两个 worker 并发索引同一文档（`knowledge_service.py:372` 先删后插互删分块） | 大扫描件 + 多 worker |
| `services/checkpoint_store.py:239` | `orphan_timeout` 从 LLM 超时推导，**不含工具/检索耗时** → 活 run 可能被判孤儿并出现在 `/runs/resumable` | 一轮里有慢工具 |
| `redis_service.py:42` + `auth.py:118` | 只有构造时兜连接失败，运行期 get/set 不兜 → Redis 挂掉后每个认证请求 500，而 `/health` 仍报 ok；且同步 Redis 调用阻塞事件循环（`get_recent_chats`、摘要、偏好同一根因） | Redis 故障 |
| `services/embedding_service.py:16` | 四个 client 构造点里**唯一没传 timeout/max_retries**（SDK 默认 read=600s×2 重试，还串行跑 N 批），而它在查询热路径上 | 内网 embedding 卡住 = 对话永久挂 |
| `knowledge_service.py:274` → `ingest_clean.py` / `ocr.py:138` | pdf/docx/xlsx 解析与 PNG 渲染是同步 CPU 调用直接跑在事件循环（全库只有 `telemetry.py:230` 用了 `to_thread`） | 一次大 PDF 上传停摆该 worker 上所有 SSE |
| `services/notification_service.py:40` | 未读去重是 check-then-insert，`models.py` 无对应唯一约束 → 并发/多 worker 出多条未读 | 并发恢复 |
| `services/cancellation.py:79` | `unregister` 无引用计数直接 `pop`，同 run 被并发驱动时先结束那个会把令牌 pop 掉，另一个的 `is_cancelled` 永远 False | 与 D1 那条孤儿竞态同源 |

### D4 待办：安全与前端（均 ⏳未复核）

- `services/egress.py:107` 命中 allowlist 即**跳过私网解析**（配 `*.corp.com` 后对方子域指 `169.254.169.254` 照放）；`:80` 只用 `is_private`，不含 `100.64.0.0/10`（阿里云元数据 `100.100.100.100`）；`:111` DNS 解析两次，TOCTOU 未防（作者已标注）。逐跳重定向与 scheme/IP 形式那半边**是防住的**（`workspace_tools.py:629` `follow_redirects=False` + 每跳 `check_url`）
- `config.py:842` `GUARDRAIL_BLOCK_SCORE=0` → `workspace_tools.py:405` 那条"注入命中就拒绝持久化"的分支是死代码，`fetch_web_page → save_to_knowledge_base` 可把注入文本落库并被之后每轮 RAG 复用
- `main.py:106` `/uploads` StaticFiles 无鉴权，且不校验归属当前用户
- `audit_log.py:56` 哈希链是真的，但 entry_hash 无密钥、`(actor, seq)` 无唯一约束 → 有 DB 写权限者可整链重算，尾部删除不可见
- `electron/main.ts:13` 无 `setWindowOpenHandler`/`will-navigate`；文档给的链接在新窗口开远程页并继承 `preload` 暴露的 `fs:pick-folder`
- 前端：通知深链只带 chatId（同会话多条挂起时点第 2 条看到第 1 张）；`document_jobs` 的 `jobStatus/jobProgress/jobAttempts` 后端已返回而前端类型未声明（排队/索引中/第3次重试/永久坏文件看起来一样）；全仓无 ErrorBoundary；CI 无 lint 步骤（现 6 errors / 28 warnings）

