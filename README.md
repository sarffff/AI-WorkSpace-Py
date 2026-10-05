# 客服工单解决 Agent · Ticket Resolution Agent

**让 Agent 真的把工单办完，而不是只回答工单。**

查订单、改地址、发起退款、开票、关单——资金类动作必须有人点头，全程可审计、可熔断、可量化。
方向基准是 [`Development_Process.md`](Development_Process.md) 的五层架构：
接入 → 理解 → 编排 → 能力 → 执行与安全，外面再包一层治理与观测。

> 这份 README 只写仓库里真有的东西。做不到的事集中在最后一节
> [诚实边界](#-诚实边界)，不在功能列表里含糊过去。

---

## 目录

- [现在能做到什么](#-现在能做到什么)
- [快速开始](#-快速开始)
- [架构与模块地图](#-架构与模块地图)
- [坐席界面](#-坐席界面)
- [开关与代价](#-开关与代价)
- [API 一览](#-api-一览)
- [数据与迁移](#-数据与迁移)
- [测试与门禁](#-测试与门禁)
- [评估](#-评估)
- [部署](#-部署)
- [诚实边界](#-诚实边界)
- [排障](#-排障)

---

## ✨ 现在能做到什么

一张工单从渠道进来：Agent 读它、判风险、按需查资料、提议要执行哪些操作、把资金类
操作挂到人面前、拿到批复之后真的去做、做完写台账并回话。中途任何一步出问题都留下
能查的痕。

- **受理** — 六个渠道归一化（`web_chat` / `email` / `app` / `wecom` / `phone` / `api`）。
  实际开放哪些由配置与已实现的适配器**取交集**：一个手抄成 `emial` 的值不会让工单
  以不存在的渠道名进库
- **去重** — 同一封邮件重投递只建一张工单（`(workspace, channel, external_ref)` 唯一）
- **身份** — 邮箱/手机号归一；多候选时**不猜也不合并**，留给工具回填
- **理解** — 订单号与金额先由确定性规则抽，模型只补齐；模型给的值必须能在原文里
  逐字对上，否则丢弃。原文超限**报错而不是截断**——订单号常常就在被削掉的那一段里
- **风险分级不问模型** — 金额阈值、法律/投诉关键词、负面情绪决定档位；金额未知一律
  按最坏情况处理
- **编排** — LangGraph 显式状态机
  `understand → retrieve → plan → reason ⇄ await_approval → execute → confirm | escalate`
- **人在回路** — `interrupt()` 挂起，跨请求、跨进程恢复；批准的参数**就是**执行的参数
- **治理** — 单工单工具调用数、单工单成本、每工作区当日退款总额、一键全局暂停
- **异常与超时** — 工具连续失败熔断、预算耗尽转人工、SLA 到点自动交接（带上"已经查到
  哪一步"）
- **出口** — 回复与邀评先进待发队列再发：租约 + 退避重试 + 幂等认领，抑制必须写理由
- **观测** — append-only 工单轨迹、哈希链审计、操作台账、五个核心指标、成本按工单归因
- **评估** — 护栏门禁在通道边界打桩、只看落库事实判分：漏投一次审批、重复退一笔钱、
  超额退款都会让 CI 红
- **桌面端** — Electron 打包，同一套界面在浏览器里也能跑

---

## 🚀 快速开始

### 要求

Python 3.11+ · Node 18+ · MySQL 8.0+ · Redis 7+（可选）· Qdrant（可选，不接则向量层降级）

### 一键（推荐）

```bash
node run.js                 # 前后端一起起，schema 落后时会询问是否迁移
node run.js backend         # 只起后端
node run.js frontend        # 只起前端
node run.js --no-migrate    # schema 落后时只报告，不动库
```

### 手动

```bash
# 1. 后端
cd back-end
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # 至少填 DATABASE_URL 与 LLM_API_KEY
alembic upgrade head
uvicorn main:app --reload --port 3000

# 2. 前端（另开终端）
cd front-end
npm install
npm run dev                                            # Vite + Electron；纯浏览器开 http://localhost:5173
```

后端跑在 `http://localhost:3000`，前端 API 基址就是这个值
（`front-end/src/shared/api/client.ts` 的默认参数），不需要额外配置。

注册一个账号登录，进来落在 `#/queue`。

### 让它真的动起来

新克隆的仓库里 **Agent 是关着的**：队列能看、工单能建，但没有任何东西会自动办。

```bash
# back-end/.env
TICKET_AGENT_ENABLED=true      # 不开这个，/tickets/{id}/run 与业务工具根本不注册
```

改完重启后端。启动日志会打印当前生效的能力面（委派模式、SOP 份数、治理限额），
这是核对配置有没有落地最快的办法。

---

## 🧱 架构与模块地图

后端在 `back-end/`。一层一个目录或一组文件，跨层调用只向下。

| 层 | 位置 | 负责什么 |
| --- | --- | --- |
| 接入 | `services/ticket/intake.py`<br>`routers/ticket_router.py`<br>`routers/attachment_router.py` | 渠道归一、投递去重、身份识别、正文与附件的量级上限 |
| 理解 | `services/ticket/understand.py` | 规则先抽实体、模型补齐并逐字回核；风险分级与意图判定 |
| 编排 | `services/ticket/graph.py`<br>`services/subagent.py` `services/agent_roles.py` | 状态机、挂起与恢复、工具面装配、子代理委派 |
| 能力 | `services/ticket/tools.py`<br>`services/retriever.py` `knowledge_service.py`<br>`services/skill_*.py` | 业务工具（读/改/资金三档）、政策检索、SOP 作业指导 |
| 执行与安全 | `services/tool_runtime.py` `services/approval.py`<br>`services/ticket/governor.py` `ledger.py` | 幂等键、参数强校验、审批参数一致性、限额与熔断、操作台账 |
| 护栏 | `services/guardrails.py` `ingest_clean.py` `egress.py`<br>`security_preflight.py` `file_types.py` | 注入定界与标记中和、摄取清洗、SSRF 出口策略、缺护栏拒绝启动 |
| 出口 | `services/ticket/outbox.py` `alerts.py`<br>`services/notification_service.py` | 回复与邀评的待发队列、坐席通知、健康告警 |
| 观测 | `services/telemetry.py` `services/ticket/trace.py`<br>`services/audit_log.py` `production_monitor.py` | span 追踪、工单轨迹、哈希链审计、线上聚合指标 |
| 成本 | `services/pricing.py` `token_budget.py` `usage_guard.py` | 价目表、按工单归因的成本、按用户的用量闸门 |
| 上下文 | `services/ticket/history.py` `services/clock.py` | 同客户/同订单/同意图的历史判据、统一时钟与时区 |
| 评估 | `eval/` | 护栏门禁（离线零成本）+ RAG 金标对照（真调用） |

### 工单图里谁拿哪些工具

工具面按 **风险档 + 已加载的 SOP** 现装配，而不是给所有人一套全量工具：

- 默认只放查询档；改与资金档要风险档够低或被显式打开
- 监督模式下主循环**不收**查询类工具，那些交给委派的子代理
- `search_policy` 只在检索真的接上时才注册
- `delegate` 永远最后注册，且委派出去的子代理工具面里**没有写操作**——审批闸门在
  主循环按提议判，委派出去的执行够不着那道门；走那条路会把 `approved_by` 填成当前
  坐席的 id，那是一次伪造而不是一次省略
- 未登记档位的新工具一律按**资金类**处理（fail-closed）

### 提示词

`back-end/prompts/` 下一个 key 一个目录、按版本分文件。契约在**启动时**校验而不是
运行时才发现（`services/prompt_library.py`）：

| key | 用途 |
| --- | --- |
| `ticket_understand` | 理解层：意图 / 实体 / 情绪 |
| `ticket_agent` | 主推理循环：提议下一步与工具调用 |
| `ticket_plan` | 办单之前的多步计划 |
| `ticket_sub_inquiry` `ticket_sub_policy` `ticket_sub_reassurance` | 三个子代理角色 |
| `eval_rag_answer` | RAG 评估里生成对照答案 |

---

## 🖥️ 坐席界面

前端在 `front-end/`，React 19 + Vite + Tailwind，无组件库。设计判断只有一条：
**一张单在等谁，扫一眼就知道**。

| 路由 | 页面 | 回答的问题 |
| --- | --- | --- |
| `#/queue` | 工单队列 | 现在有什么、什么在等我。顶部值班板四格：全队列 / 等你批 / 超期未闭 / 本轮回收 |
| `#/tickets/:id` | 详情与轨迹回放 | 客户说了什么、抽出了哪些事实、Agent 办到哪一步、花了多少钱 |
| `#/approvals` | 审批收件箱 | 哪个资金操作在等点头，改哪个参数 |
| `#/governance` | 治理台 | 当前限额、一键暂停、待发队列、待人工纠正的写操作 |
| `#/metrics` | 指标看板 | 文档§1 那五个核心指标 |
| `#/knowledge` | 知识库 | 政策文档上传、切块、dense / sparse 命中调试 |
| `#/skills` | 作业指导 | SOP 的增删改与版本 |
| `#/notifications` | 通知 | 持久收件箱：挂审与交接都进这里 |
| `#/audit` | 审计 | 哈希链审计与完整性校验 |
| `#/workspace` | 工作区 | 成员、邀请码、角色 |

### 视觉与主题

- **浅色 / 深色 / 跟随系统** 三态，存在 `localStorage` 的 `app-theme`，在首帧之前应用，
  所以切主题不会白闪一下
- 颜色全部走 CSS 变量（`src/app/styles/index.css`），Tailwind 只做映射；深色下状态色
  整体提亮一档——浅色那套放在 `#141413` 上对比不足，而"等人批"这类信号色丢了对比
  等于丢了这个功能
- 队列是**账本**不是卡片墙：等宽数字、行分隔线、左沿一条竖色就是权限档位
  （读 / 改 / 资金），"等你批"的药丸带呼吸灯
- **未知是一等公民**：取不到用量、没标注纠正率时显示斜体"未知"而不是 0，旁边写明
  "未知不是免费"
- 键盘焦点可见（坐席里真的有人全程用键盘）

---

## 🎛️ 开关与代价

默认值一律偏保守：新能力先关着进仓库，"上线出问题"要能用一条配置拨回去，而不是
回滚部署。

| 键 | 默认 | 打开它的代价 |
| --- | --- | --- |
| `TICKET_AGENT_ENABLED` | `false` | 工单域总开关。关着时工单路由与业务工具都不注册 |
| `TICKET_CHANNELS` | `web_chat,email,app,api` | 与已实现适配器取交集；写进配置不会让没实现的渠道自己收到流量 |
| `TICKET_UNDERSTAND_LLM` | `true` | 关掉只剩规则层抽取：商品名与情绪判定就没有了，是一个**明确**的降级面 |
| `TICKET_PLAN_ENABLED` | `true` | 多一次调用。计划的读者不是模型（循环本来就能随机应变），是审批人 |
| `SKILL_ENABLED` | `false` | 每张工单多一条索引 system 消息；正文按需 `load_skill`，成本取决于加载几份 |
| `AGENT_DELEGATION_MODE` | `off` | 每次委派是一整个嵌套子代理循环，成本与延迟都上去。先在轨迹里看清哪一步不够用再开 |
| `GUARDRAIL_ENABLED` | `true` | 生产环境关掉它 + 开着 Agent = **拒绝启动**，这是刻意的 |
| `TICKET_MAX_TOOL_CALLS` | `12` | 单工单工具调用上界，子代理内部的调用也计入 |
| `TICKET_DAILY_REFUND_LIMIT` | `1000` | 每工作区当日退款总额（按应用时区的自然日）。0 = 一律不许自动退 |
| `TICKET_MAX_COST_PER_TICKET` | `0`（不限） | 建议上线就设。超了就地收尾并转人工 |
| `TICKET_REFUND_REVIEW_THRESHOLD` | `200` | 达到即进人审。设 0 = 任何退款都要人批 |
| `TICKET_SLA_HOURS` | `24` | 0 = 不设 SLA，队列上会显示"不设时限" |
| `VECTOR_STORE` | `memory` | 生产建议 `qdrant`；memory 是进程内的，重启即重建 |
| `TICKET_CSAT_INVITE_ENABLED` | `true` | 办结后排一条邀评。没接发送通道时只是往队列多放一行 |

完整清单在 `back-end/.env.example`（带每条的理由注释）。

---

## 🔌 API 一览

响应是 camelCase，请求体是 snake_case。一个例外要知道：
`GET /tickets/{id}/pending` 的中断载荷**保持 snake_case**——它是检查点里存的原样，
不是给前端塑形用的 DTO，改它会和恢复路径上的重放对不上。

### 工单 `/tickets`

```
POST   /tickets                         提交工单（渠道归一化 + 去重 + 身份识别）
GET    /tickets                         队列：状态 / 风险 / SLA，顺带回收超期工单
GET    /tickets/{id}                    详情（entities 已解析成对象）
GET    /tickets/{id}/events             轨迹回放（append-only）
POST   /tickets/{id}/run                跑编排，返回这一次的终态
POST   /tickets/{id}/decision           人工裁决：同意 / 改参数同意 / 拒绝
POST   /tickets/{id}/csat               客户满意度回写
POST   /tickets/{id}/close              人工关闭
GET    /tickets/pending                 待审批收件箱（从检查点读，跨进程可查）
GET    /tickets/metrics                 五个核心指标
GET    /tickets/outbox                  待发队列
POST   /tickets/outbox/drain            手动排空一次
POST   /tickets/outbox/{row_id}/suppress  抑制一条外发（必须写理由）
GET    /tickets/governor/state          当前限额
POST   /tickets/governor/pause          一键暂停
POST   /tickets/governor/resume         恢复
GET    /tickets/operations/unreviewed   待人工纠正的写操作
POST   /tickets/operations/{id}/review  标注"这次操作对不对"
```

### 其余

| 前缀 | 端点 |
| --- | --- |
| `/auth` | `register` `login` `me` `logout` `refresh` `oauth/{github,google,callback}` |
| `/knowledge` | `documents` `documents/upload` `documents/from-url` `query`（混合检索调试） |
| `/skills` | `GET` 列表（内置 + 工作区）· `PUT` upsert · `DELETE /{id}` |
| `/attachments` | `upload`（工单附件，白名单与类别判定同源） |
| `/notifications` | `GET` `unread_count` `{id}/read` `read_all` |
| `/audit` | `GET` 列表 · `GET /verify` 哈希链完整性校验 |
| `/metrics` | `usage` `health` `traces` `traces/{id}` |
| `/workspace` | `GET` `join` `invite-code` `members/{id}` |

### MCP

同一套业务工具也能以 stdio 对外给别的 Agent 用：

```bash
cd back-end && python -m services.ticket.mcp_server
```

---

## 🗄️ 数据与迁移

MySQL（SQLAlchemy 2 + Alembic），当前 head `0025_ticket_attribution`，25 个 revision。

| 表 | 存什么 |
| --- | --- |
| `tickets` `ticket_events` | 工单本体与 append-only 轨迹 |
| `cs_customers` `cs_orders` `cs_order_items` `cs_shipments` `cs_refunds` `cs_invoices` | 业务系统侧数据，工具对着它们读写（当前是本地替身） |
| `cs_operations` | 写操作台账：幂等键、结果、是否经人工纠正 |
| `cs_governors` | 限额与暂停状态，按工作区 |
| `ticket_outbox` | 待发外发：租约、尝试次数、下一次可发时间 |
| `trace_spans` `audit_log` `notifications` | 观测与审计（都挂 `ticket_id`） |
| `documents` `document_chunks` `document_jobs` | 政策库与持久索引队列 |
| `workspace_skills` | SOP 正文与版本 |

```bash
cd back-end
alembic upgrade head                     # 迁移
alembic heads                            # 确认只有一个 head
python scripts/check_schema_drift.py     # 模型与库不一致就非零退出
python reset_db.py                       # 开发期重建（会清库，慎）
```

**检查点不在 MySQL 里**：挂在人审上的工单状态存在 `TICKET_CHECKPOINT_DB` 指向的
sqlite 文件。它是运行数据、活得要比进程长，多实例部署必须换成共享存储或 Postgres
saver——两个进程各写一份 sqlite，会得到两条互不知情的挂起线程。启动时按
`WEB_CONCURRENCY>1` 打这条告警；那个 env 不是每种起法都设，所以它是提示不是保证，
兜底的仍是部署文档。

---

## 🧪 测试与门禁

```bash
# 后端 1077 条
cd back-end && python -m pytest

# 提示词契约：模板缺失或占位符漂移直接失败
python -c "from services import prompt_library; prompt_library.validate()"

# schema 漂移：模型与库对不上就非零退出
python scripts/check_schema_drift.py

# 工单护栏门禁（离线、不花钱）
python -m eval.ticket_guardrail --fail-on-breach

# 前端：类型 + 单测 20 条 + lint
cd front-end && npx tsc --noEmit && npm test && npm run lint
```

CI（`.github/workflows/ci.yml`，push 到 `main`/`dev` 与提 PR 时）跑的是其中两半：

- **backend job** —— 提示词契约校验 + `pytest`
- **frontend job** —— `tsc --noEmit` + `vitest` + `vite build`（pnpm 11 + Node 22，
  版本原因写在 workflow 的注释里）

**schema 漂移与护栏门禁不在 CI 上**：前者需要一个活的 MySQL，后者虽然离线但也还没挂。
改了工单图、护栏或 outbox 时请手动跑 `eval.ticket_guardrail`；改了模型、`models.py`
或迁移时手动跑 `check_schema_drift.py`。

护栏门禁现在是 **12 条用例**，判分只看**落库事实**而不是模型自述：该挂审的挂了吗、
退款重复了吗、超额了吗、委派有没有绕过审批（`delegated-write-cannot-bypass-review`）。
漏投一次审批就非零退出。

### 测试写法

用例名用中文（`test_当金额超过阈值时必须挂审`），一条测试对应一个真实事故或一条
不变量。守卫类测试的 docstring 必须写清**防的是哪次事故**——否则下一个人只会把它
当成噪声删掉。像 `test_file_types_single_source.py` 那样"扫源码看有没有人又抄了一份
清单"的结构守卫是刻意写的：清单副本一开始都相等，它们是**后来**才漂移的，所以断言
要落在结构上而不是值上。

---

## 📊 评估

两个评估面，各自回答一个问题：

| 面 | 回答的问题 | 花钱吗 |
| --- | --- | --- |
| `eval/ticket_guardrail.py` | 工单处置的护栏漏没漏 | 不花（脚本化替身，只看落库事实） |
| `eval/run.py` | 改了检索配置，政策库的召回变好还是变坏 | 花（真实调用模型与 embedding） |

```bash
cd back-end
python -m eval.ticket_guardrail --fail-on-breach    # 门禁，CI 跑这个

python -m eval.run --limit 5                        # 小样本试跑，先确认链路通
python -m eval.run --variants baseline,dense-only,rerank
python -m eval.run --repeat 5 --temperature 0.7     # 量方差：报告出 cv / stdev
python -m eval.gate                                 # 判门禁，阈值在 eval/gate_thresholds.json
```

报告写到 `eval/reports/`：`.md` 是对照表，`.json` 是逐题明细（含裁判理由与完整回答）。

`eval.gate` 的退出码分开是刻意的：`1` 质量或安全回归、`2` **运行本身不可信**、
`3` 用法错误。`2` 单独一档是因为限流打空答案、语料分块数变了这类情况会让所有质量列
归零——那时看起来像模型彻底坏了，该做的其实是修环境重跑，而不是去查模型。

真跑一次之前先配好价目表，否则成本列全是"未知"：

```bash
cd back-end && cp model_prices.example.json model_prices.json   # 按你实际用的模型改
```

---

## 🚢 部署

```bash
# 后端容器（python:3.11-slim，监听 8000，健康检查 GET /health）
cd back-end && docker build -t ticket-agent . && docker run --env-file .env -p 8000:8000 ticket-agent

# 向量库
docker compose -f docker-compose.qdrant.yml up -d

# 桌面端
cd front-end && npm run package:win     # 也有 :mac / :linux
```

### 上生产之前

`ENV=production` 时启动体检有问题就**拒绝启动**，不是打日志了事。清单：

- JWT 不是占位值且 ≥32 字符
- `LLM_API_KEY` 不是占位值且非空
- `DATABASE_URL` 不是示例口令 `root:password`
- Qdrant 开在非本机地址时必须带 `QDRANT_API_KEY`
- **`TICKET_AGENT_ENABLED=true` 时 `TICKET_DAILY_REFUND_LIMIT` 必须是正数**

最后一条和前面几条同等待遇，因为它的故障不是报错，而是账上少了一笔没人记得是谁批的
钱。这类事写在 README 里没用，约束要落在启动闸门上。

---

## ⚠️ 诚实边界

以下是**现在做不到**的事。写在这里是因为它们每一个都看起来像已经做到了。

- **真实入口只有一条** —— `/tickets`。`TICKET_CHANNELS` 里的 `email` 说的是"这个渠道
  的归一化形状支持"，不是"能收到邮件"：email / wecom / phone 的**收件**适配器还没做
  （转写与回调都没有）
- **外发没有接任何真实通道** —— 邮件 / 企微 / 短信都没有。待发队列会如实回答
  "没有接入任何发送通道"，而不是假装已送达。这条是 `TICKET_OUTBOX_*` 那一套
  租约/重试/幂等存在的理由，也是它今天还没有对手方的原因
- **业务系统是本地替身** —— `cs_orders` / `cs_refunds` 这些表在同一个库里。接上真实
  ERP 与支付之前，轨迹里那句"已执行"意思是"本地台账记了一笔"
- **委派默认关闭** —— 代价是每次委派多一整个子代理循环，而"哪一步不够用"要先在工单
  轨迹里看得见才有依据调它。**写操作任何模式都不进子代理工具面**：审批闸门在主循环
  按提议判，委派出去的执行够不着那道门
- **SOP 的"必备材料"目前只是登记** —— `required_inputs` 与版本号会存库、会在界面上
  给 admin 看，但 `load_skill` 交给模型的只有正文。也就是说"缺材料就别下结论"仍然是
  正文里的一句**措辞**，不是代码里的约束。它曾经有个强制消费方（结构化审核结论按声明
  的项数生成必填核对槽），随报销审核栈一起删掉了
- **SOP 不能逐版追溯** —— `workspace_skills` 只存最新一份正文，版本号回答"改过几次"，
  回答不了"当时那版写了什么"
- **前端不实时** —— 没有 SSE 也没有 WebSocket。队列与收件箱靠进入页面时拉取 + 手动
  刷新；通知是持久收件箱（不依赖易失连接），但页面不会自己跳
- **上传框不设 accept** —— 文件类型白名单的唯一真相源在 `services/file_types.py`，
  `payload()` 已经备好却没有任何端点发布它（原先那个 `/settings` 随对话工作台删了）。
  所以前端刻意不写第二份清单，不收的类型由后端在摄取时拒掉
- **工单附件只保存不解析**，不进检索。要 Agent 看内容得先转成文字
- **历史相似工单是判据查询**（同客户 / 同订单 / 同意图），不是语义相似
- **成本取不到就是"未知"** —— 模型不在价目表里、或那次调用没回传用量时，
  `llmCost` 是 `null`。UI 会显示成斜体"未知"并写明"未知不是免费"，不会折成 0
- **评估要真调模型** —— 所以它不在 PR 上跑，另有每天一次的定时任务

---

## 🔧 排障

| 现象 | 先看哪里 |
| --- | --- |
| 前端一直转圈 / 网络错误 | 后端起在 `:3000` 了吗。API 基址写死在 `front-end/src/shared/api/client.ts` 的构造参数 |
| 队列是空的，`/tickets` 返回 404 | `TICKET_AGENT_ENABLED` 没开——工单路由与业务工具都不注册 |
| 403 且提示需要管理员 | 工作区角色。成员与邀请码在 `#/workspace` |
| 429 | 按用户的用量闸门（频率 / 成本 / token）：`services/usage_guard.py` |
| 工单卡在"等你批"不动 | 那是在等人，不是故障。去 `#/approvals`；`GET /tickets/pending` 从检查点读 |
| 工单突然变成"已转人工" | SLA 到点自动交接，或预算/失败次数熔断。轨迹里那条 `escalate` 写了原因 |
| 启动就报 `TICKET_DAILY_REFUND_LIMIT...` | `ENV=production` 下的护栏体检，见[上生产之前](#-上生产之前) |
| 启动报提示词契约失败 | `prompts/` 下的模板或占位符漂了。`PROMPT_*_VERSION` 要与目录里的版本对得上 |
| 模型没问题但报 schema 落后 | `python scripts/check_schema_drift.py`，然后 `alembic upgrade head` |
| 指标页全是"未知" | 没配 `model_prices.json`，或那次运行没回传用量。见[评估](#-评估)末尾 |
| 登录态反复失效 | 改过 `JWT_SECRET_KEY`，旧 token 与 refresh 链一起作废，重新登录 |
| 挂起的工单恢复后回到挂起前 | 多实例 / 多 worker 共写一份 sqlite 检查点，见[数据与迁移](#-数据与迁移) |

### 提交约定

Conventional Commits + 中文描述，和 `git log` 一致：
`feat(编排): 子代理接进工单图`、`fix(前端): 详情页 entities 是对象不是字符串`。
范围写模块而不是写文件。

---

## 📄 许可

仓库里目前没有 `LICENSE` 文件。需要开源或对外授权时先补一个，别默认"没写就是随用"。
