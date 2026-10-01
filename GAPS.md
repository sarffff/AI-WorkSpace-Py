# 生产化缺口与已知问题清单

> 范围：本轮补齐 P0（取消 / 模型 Fallback / Office 解析 / URL 入库 / 索引重启恢复 / 能力档案）**之后**仍未覆盖的部分。
> 分三类：**A. 已实现功能的边界（技术债）**、**B. 未实现的功能缺口（P1/P2）**、**C. 探索中发现的具体隐患**。
> 判据沿用 `standard.md` 的"生产级 Agent 十二条能力"。

---

## A. 本轮 P0 已实现、但有明确未覆盖情形（技术债）

这些不是 bug，是**刻意收窄的边界**，代码注释与 `.env.example` 里都写明了。列出来是为了下一步知道从哪接。

| 编号 | 项 | 已做到 | 未覆盖 | 影响 |
|---|---|---|---|---|
| A1 | 取消/中断（P0-1） | 单进程内 `asyncio.Event`，循环在轮首/工具前收尾成 `cancelled` | **跨 worker 叫停活循环**：内存事件跨不了进程。多 worker 部署时，取消请求打到 worker B、而循环在 worker A，A 上的活循环收不到事件 | 仅当切到 Qdrant 多 worker 后才暴露；`mark_cancelled` 已能落库终态，UI 正确，但循环要跑到自然结束 |
| A2 | 主模型 Fallback（P0-2） | 同 endpoint 换模型名，开流前切换，成本按实际模型记账 | **跨提供商故障切换**：整个提供商宕机（base_url/key 级）时无备用；需 per-provider 凭证列表 | 提供商整体不可用 = 主回答仍报错 |
| A3 | Office 解析（P0-4a） | pdf / docx / xlsx / pptx 结构化解析入库；**扫描件 PDF OCR 已补**（视觉模型逐页转写，默认关，`INGEST_OCR_*`，见 `services/ocr.py`） | **图片型 docx/pptx 的内嵌图 OCR**：常是 logo 而非内容页、质量存疑，本轮未做，仍落 `no_extractable_text` | 扫描件 PDF 配了视觉模型即可入库；图片型 Office 文档仍进不了库 |
| A4 | 索引重启恢复（P0-3） | 启动时重跑卡在 `processing` 的文档（幂等） | **完整持久任务队列**：仍是进程内 `BackgroundTasks`；无进度上报、无并发上限、无失败重试策略；Qdrant 未转默认 | 大批量导入时无进度/背压；多 worker 仍需先切 Qdrant |
| A5 | 能力档案（P0-5） | `/settings` 集中只读上报 + 设置页展示 | **运行时切换**：全是进程级 `.env` 配置，改完要重启；没有"配置档案一键切换 + 校验"的引导流程 | 运营方拼一个可用 Agent 仍需读懂 `.env.example` 并手工配对提示词版本 |

---

## B. 未实现的功能缺口

### P1 — 影响体验与可持续运营

| 编号 | 缺口 | 现状 | 说明 |
|---|---|---|---|
| B1 | **通知系统** | 完全没有 | 审批挂起后无任何推送，只能靠用户回到同一会话页；24h 无人裁决即 `abandoned`。HITL 是核心卖点，审批人不在场时闭环断裂。需站内 + IM/邮件 |
| B2 | **多租户 / 协作 / RBAC** | 单工作区 + `admin`/`user` 两级 | 无细粒度角色（编辑者/审批人/只读审计员分离）、无租户级配额（`usage_guard` 只到用户级）、Agent 无独立身份与短期凭证（直接用发起人权限） |
| B3 | ~~**评估线上回流 + 告警**~~ ✅ | 离线金标强（54题RAG/29任务Agent+门禁）+ 线上差评→回归用例；**主动告警**（`services/production_monitor.py`，默认关）；**线上抽样送评**（`eval/online_sample.py`：真实问答重检索后送 AnswerJudge，低分导成回归候选）；**多次运行看方差**（`eval.run --repeat N --temperature 0.7`，报告出 cv/stdev 小节） | 本轮补齐。遗留：抽样重检索的 context 是当前库而非当初那条回答真用的（faithfulness 会漂，relevance 准），已写进模块文档 |
| B4 | **会话回放 UI** | 数据齐（span 树 + 最近 8 份 checkpoint 快照） | 缺产品化：没有把快照渲染成时间线回放、没有"回到第 N 轮重跑"入口。`AGENT_CHECKPOINT_KEEP` 注释说"留多份是为了重放"，但重放入口没做 |
| B5 | **回答级操作** | 无 | 缺 regenerate（重新生成）、编辑后重发、分支对话、引用点击跳原文、对话导出/分享。属聊天产品及格线 |

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
5. **B4 回放 UI + B5 regenerate/编辑重发**（数据已就绪，差产品化最后一步）
6. **A4 Qdrant 转默认 + 持久队列**（为多 worker / 批量导入铺路，同时解 A1 跨 worker 取消）

> A1（跨 worker 取消）与 A4（多 worker 前提）是同一个约束、同一个时机：切到 Qdrant 多 worker 时一起补。
