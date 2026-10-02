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
