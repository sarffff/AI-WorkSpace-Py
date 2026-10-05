# 客服工单解决 Agent (Ticket Resolution Agent)

<div align="center">

![目标](https://img.shields.io/badge/目标-真正解决工单-blue?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-14F195?style=for-the-badge&logo=langchain&logoColor=black)

**让 Agent 真的把工单办完，而不是只回答工单**

查订单、改地址、发起退款、开票、关单——高风险动作必须有人点头，全程可审计、可熔断、可量化。

[快速开始](#快速开始) · [功能特性](#功能特性) · [技术栈](#技术栈) · [API 文档](#api-文档)

</div>

---

## 📋 目录

- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [环境配置](#环境配置)
- [开发指南](#开发指南)
- [API 文档](#api-文档)
- [部署说明](#部署说明)
- [常见问题](#常见问题)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

---

## ✨ 功能特性

按 `Development_Process.md` 的五层架构组织：接入 → 编排 → 能力 → 执行与安全 → 数据与记忆。

### 📥 接入与理解

- ✅ **多渠道归一化** - web_chat / email / app / wecom / phone / api，配置取交集生效
- ✅ **投递去重** - 同一封邮件重投递只建一张工单（`(workspace, channel, external_ref)` 唯一）
- ✅ **身份识别** - 邮箱/手机号归一；多候选时**不猜也不合并**，留给工具回填
- ✅ **原文不削尾** - 超限直接报错而不是截断：订单号常常就在被削掉的那一段里
- ✅ **规则先行的实体抽取** - 订单号/金额先由确定性规则抽，模型只补齐；
  模型给出的订单号与金额必须能在原文里逐字对上，否则丢弃
- ✅ **风险分级不问模型** - 金额阈值、负面情绪、法律/投诉关键词决定风险档；
  金额未知一律按最坏情况处理

### 📚 政策知识库（RAG）

- ✅ **文档管理** - 上传、索引、按工作区/个人两级可见性隔离
- ✅ **混合检索** - 稠密向量 + BM25，RRF 融合，可选重排（cross-encoder / API）
- ✅ **持久索引队列** - 认领 + 租约 + 退避重试，进程重启不丢正在索引的文档
- ✅ **注入防护** - 政策片段与客户原文进上下文前都过护栏（定界 + 标记中和）

### 🔀 编排层（LangGraph 状态机）

- ✅ **显式流程** - understand → retrieve → plan → reason ⇄ await_approval → execute → confirm | escalate
- ✅ **真人在回路** - `interrupt()` 挂起，跨请求、跨进程恢复；批准的参数**就是**执行的参数
- ✅ **检查点** - AsyncSqliteSaver 落盘，"点了同意但进程重启"不会丢单
- ✅ **提示词按 key 版本化** - 工单理解 / 操作说明 / 规划各有独立版本，契约校验在启动时炸而不是运行时
- ✅ **异常与超时** - 工具连续失败熔断、预算耗尽转人工、SLA 到点自动交接（带上"已经查到哪一步"）

### 🛠️ 能力层

- ✅ **业务系统工具** - 查订单 / 查物流 / 查客户 / 改地址 / 开票 / 退款 / 取消，
  按查询-修改-资金三档分级，默认只给查询档
- ✅ **MCP Server** - 同一套工具通过 `python -m services.ticket.mcp_server` 以 stdio 对外
- ✅ **SOP Skills** - 版本化作业指导（内置 `refund-playbook`），工作区可自写同名覆盖
- ✅ **通知** - 待审批与交接都会写持久收件箱，不依赖那条易失的 SSE 连接

### 🛡️ 执行与安全

- ✅ **幂等键** - 写操作先查幂等再判业务状态；键由系统推导，不靠模型发明
- ✅ **参数强校验** - Pydantic + JSON Schema，必填/未知键/类型不符都挡在执行之前
- ✅ **限额与熔断** - 单工单工具调用数、单工单成本、当日退款总额、一键全局暂停
- ✅ **完整审计** - 哈希链审计 + 工单轨迹（append-only）+ 操作台账，各自回答不同问题
- ✅ **缺护栏就拒绝启动** - 开着 Agent 却没有当日退款上限时，生产环境起不来

### 📊 观测与指标

- ✅ **工单台接口** - 队列（状态 / 风险 / SLA）、待审批收件箱、轨迹回放、指标看板
- ✅ **五个核心指标** - 自动解决率、首次响应、平均处理时长、CSAT、错误操作率
- ✅ **人工纠正台账** - 让"错误操作率"有真实数据源，而不是模型自报；未标注时返回 `None` 而不是 0
- ✅ **成本按工单归因** - trace span 挂 `ticket_id`；用量取不到时返回"未知"而不是折成 0
- ✅ **线上健康告警** - 失败率 / 人工介入率 / 单工单成本 / p95 处理时长越阈值时推给管理员
- ✅ **护栏评估** - `eval/ticket_guardrail.py` 在通道边界打桩、只看落库事实判分：
  漏投一次审批、重复退一笔钱、超额退款都会让门禁红
- ✅ **RAG 金标评估与门禁** - 政策检索的质量回归，区分"模型变差"与"测量坏了"

### 📤 回复出口

- ✅ **待发队列** - 回复与邀评先落库再发，租约 + 退避重试 + 幂等认领
- ✅ **抑制必须写理由** - 不发是一个决定，不是一次事故
- 🚧 **真实发送通道未接**（邮件 / 企微 / 短信）：没有通道时一行状态都不改，
  队列会回答"没有接入任何发送通道"，而不是假装已送达

### ⚠️ 当前边界（诚实清单）

- 子代理（查询 / 政策 / 情绪安抚）与 SOP skill 尚未接进工单图：模块在，缺调用方
- email / wecom / phone 的**收件**适配器未做，目前只有 `/tickets` 这一条真实入口
- 工单附件只保存不解析，不进检索
- 历史相似工单是判据查询（同客户 / 同订单 / 同意图），不是语义相似
- 前端仍是知识库工作台那一套页面，工单台 UI 待重定位

---

---

## 🛠️ 技术栈

### 后端 (Python)

| 技术          | 版本    | 用途         |
| ------------- | ------- | ------------ |
| Python        | 3.11+   | 核心语言     |
| FastAPI       | 0.111.0 | Web 框架     |
| SQLAlchemy    | 2.0.31  | ORM          |
| MySQL         | 8.0+    | 主数据库     |
| Redis         | 7.0+    | 缓存和会话   |
| OpenAI SDK    | 4.52.7  | LLM API 调用 |
| SSE-Starlette | 2.1.0   | 流式响应     |
| Pydantic      | 2.8.2   | 数据验证     |

### 前端 (TypeScript)

| 技术          | 版本    | 用途     |
| ------------- | ------- | -------- |
| React         | 19.0    | UI 框架  |
| TypeScript    | 5.5     | 类型系统 |
| Electron      | 31.2    | 桌面应用 |
| Redux Toolkit | 2.2.6   | 状态管理 |
| Vite          | 5.3.3   | 构建工具 |
| TailwindCSS   | 3.4.4   | 样式框架 |
| Lucide React  | 0.408.0 | 图标库   |

## 🚀 快速开始

### 前置要求

- **Python** 3.11 或更高版本
- **Node.js** 18.0 或更高版本
- **MySQL** 8.0 或更高版本
- **Redis** 7.0 或更高版本 (可选)
- **npm** 或 **yarn** 或 **pnpm**

### 1️⃣ 克隆项目

```bash
git clone <repository-url>
cd AI-Workspace-py
```

### 2️⃣ 配置数据库

创建 MySQL 数据库:

```sql
CREATE DATABASE ai_workspace CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 3️⃣ 后端设置

```bash
# 进入后端目录
cd back-end

# 创建虚拟环境 (推荐)
python -m venv venv

# 激活虚拟环境
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
# 复制 .env.example 为 .env 并修改配置
cp .env.example .env
# 编辑 .env 文件,填入数据库连接、API Key 等配置

# 初始化数据库表
# 首次运行时会自动创建表

# 启动后端服务
uvicorn main:app --reload --port 3000
```

后端服务将在 `http://localhost:3000` 启动

### 4️⃣ 前端设置

打开新的终端窗口:

```bash
# 进入前端目录
cd front-end

# 安装依赖
npm install
# 或使用 yarn
yarn install
# 或使用 pnpm
pnpm install

# 启动开发服务器
npm run dev
# 或使用 yarn
yarn dev
# 或使用 pnpm
pnpm dev
```

前端开发服务器将自动打开 Electron 应用

### 5️⃣ 打包应用

```bash
# 在 front-end 目录下
npm run build           # 构建前端资源
npm run package         # 打包桌面应用

# 针对特定平台打包
npm run package:win     # Windows
npm run package:mac     # macOS
npm run package:linux   # Linux
```

打包后的应用在 `front-end/release/` 目录

---

## ⚙️ 环境配置

### 后端环境变量 (.env)

在 `back-end/.env` 文件中配置:

```env
# 数据库配置
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/ai_workspace

# 服务器配置
PORT=3000

# LLM API 配置
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
LLM_MODEL=glm-4.5-air

# Redis 配置 (可选)
REDIS_URL=redis://localhost:6379/0
```

完整配置见 `back-end/.env.example`——每一项都带了「为什么是这个值」的说明，
其中有几处是踩过坑之后写下的（超时的流式语义、辅助调用为什么单独收紧、
用量闸门为什么按用户而不按 IP）。默认值可以直接跑；Agent 的工具开关默认全关。

### 支持的 LLM 提供商

项目使用 OpenAI SDK 格式,支持以下提供商:

- **智谱 AI (GLM)** - 推荐
  - Base URL: `https://open.bigmodel.cn/api/paas/v4/`
  - 模型: `glm-4.5-air`, `glm-4-flash` 等

- **OpenAI**
  - Base URL: `https://api.openai.com/v1/`
  - 模型: `gpt-4`, `gpt-3.5-turbo` 等

- **其他兼容 OpenAI API 的提供商**
  - DeepSeek, Moonshot, 等

---

## 📖 API 文档

### 后端 API 端点

服务器启动后访问自动生成的 API 文档:

- **Swagger UI**: http://localhost:3000/docs
- **ReDoc**: http://localhost:3000/redoc

### 主要端点

#### 工单（接入 → 处置 → 治理）

```
POST   /tickets                        # 提交工单（渠道归一化 + 去重 + 身份识别）
GET    /tickets                        # 队列：状态 / 风险 / SLA，顺带回收超期工单
GET    /tickets/{id}                   # 详情
GET    /tickets/{id}/events            # 轨迹回放（append-only）
POST   /tickets/{id}/run               # 跑编排（SSE）；挂在审批上时返回 awaiting_approval
POST   /tickets/{id}/decision          # 人工裁决：同意 / 改参数同意 / 拒绝
GET    /tickets/pending                # 待审批收件箱（从检查点读，跨进程可查）
GET    /tickets/metrics                # 文档§1 那五个核心指标
GET    /tickets/governor               # 当前限额；POST .../pause 与 .../resume 是一键闸门
GET    /tickets/operations/unreviewed  # 待人工纠正的写操作
POST   /tickets/operations/{id}/review # 标注"这次操作对不对"
GET    /tickets/outbox                 # 待发回复；POST .../drain 抽干队列
```

#### 政策知识库与作业指导

```
GET    /knowledge/documents            # 文档列表（工作区共享 / 个人私有）
POST   /knowledge/documents/upload     # 上传并排队索引
POST   /knowledge/documents/from-url   # 从 URL 抓政策页（SSRF 由 egress 兜）
GET    /skills                         # 内置 + 本工作区自写的 SOP
PUT    /skills                         # admin 写自己的 SOP（同名覆盖内置）
POST   /attachments/upload             # 工单附件上传
GET    /notifications                  # 坐席收件箱（待审批 / 交接 / 健康告警）
GET    /audit                          # 哈希链审计，verify 能定位断点
GET    /metrics/usage                  # token 与成本按模型/环节聚合
GET    /metrics/health                 # 线上健康快照（admin）
GET    /metrics/traces                 # 编排运行的埋点树，按 ticket_id 过滤
```

### 请求示例

#### 提交并跑一张退款工单

```bash
curl -X POST http://localhost:3000/tickets \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"channel": "web_chat", "content": "订单 AB-12345 的鞋子磨脚，要退款 299 元"}'

# 跑编排。资金类操作会挂起等人批，返回 outcome=awaiting_approval
curl -X POST http://localhost:3000/tickets/$TICKET_ID/run \
  -H "Authorization: Bearer $TOKEN"
```

#### 批准后接着跑

```bash
# 同意时改参数：<call_id> 来自上一步返回里那条待批内容
curl -X POST http://localhost:3000/tickets/$TICKET_ID/decision \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"approved": true, "note": "按政策上限退", "edited": {"<call_id>": {"amount": "289"}}}'
```

---

## 🔧 开发指南

### 开发规范

- **代码风格**: 遵循 PEP 8 (Python) 和 ESLint (TypeScript)
- **提交规范**: 使用语义化提交信息
- **分支策略**: Git Flow

### 测试与门禁

```bash
# 后端(1692 条)
cd back-end && python -m pytest

# 提示词契约:模板缺失或占位符漂移直接失败
python -c "from services import prompt_library; prompt_library.validate()"

# 前端(140 条)
cd front-end && pnpm test && pnpm tsc --noEmit
```

这三样在每次 push 与 PR 上跑（`.github/workflows/ci.yml`）。

**评估不在 PR 上跑**——它打真实模型调用，一次几分钟、花真钱。改了检索、
提示词或语料之后手动触发，另有每天一次的定时跑
（`.github/workflows/eval.yml`）：

```bash
# 跑评估
cd back-end && python -m eval.run --variants baseline,rerank-api

# 判门禁(阈值在 eval/gate_thresholds.json)
python -m eval.gate

# 多次运行看方差：同一套题在生产温度下重跑 N 次，报告出 cv/stdev
# （离线默认温度 0 几乎不抖，温度 >0 才量得出线上那种波动）
python -m eval.run --repeat 5 --temperature 0.7

# 线上抽样送评：把真实问答回流进裁判打分，低分导成回归候选
# （补"离线金标 ↔ 线上分布"那半环；重检索用当前库，relevance 准、faithfulness 会漂）
python -m eval.online_sample --limit 50 --export
```

门禁退出码分开是刻意的：`1` 质量或安全回归、`2` **运行本身不可信**、
`3` 用法错误。`2` 单独一档是因为限流打空答案、语料分块数变了这类情况会让
所有质量列归零——那时候看起来像模型彻底坏了，实际该做的是修环境重跑，
而不是去查模型。

### Agent 评估

```bash
cd back-end && python -m eval.run_agent --variants baseline
```

29 个任务，量的是工具召回、轮次效率、以及委派的开销。委派默认关闭的依据就在
这里：`eval/reports/agent-eval-*.md`。

### 添加新功能

1. **后端添加 API**

```python
# back-end/routers/your_router.py
from fastapi import APIRouter

router = APIRouter(prefix="/your-feature", tags=["your-feature"])

@router.get("/endpoint")
async def your_endpoint():
    return {"message": "success"}
```

2. **前端添加类型**

```typescript
// front-end/src/shared/types/api.types.ts
export interface YourType {
  id: string;
  name: string;
}
```

3. **前端添加 API 方法**

```typescript
// front-end/src/shared/api/client.ts
async getYourData(): Promise<YourType[]> {
  const response = await fetch(`${this.baseUrl}/your-feature/endpoint`)
  return response.json()
}
```

### 调试技巧

#### 后端调试

```bash
# 启用详细日志
uvicorn main:app --reload --log-level debug
```

#### 前端调试

- 使用 Chrome DevTools
- 使用 Redux DevTools 查看状态
- 查看 Electron 主进程日志

---

## 📦 部署说明

### Docker 部署 (推荐)

```bash
# TODO: 添加 Dockerfile 和 docker-compose.yml
```

### 手动部署

#### 后端部署

```bash
# 使用 Gunicorn + Uvicorn Workers
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:3000
```

> **多 worker（`-w` > 1）必须切 Qdrant。** 默认 `VECTOR_STORE=memory` 的索引是
> 每个 worker 进程各建一份、从 MySQL 派生：一次上传只在服务了那个请求的 worker 上
> 生效，之后能不能检索到取决于下个请求打到谁——表现为"刚传的文档一半时间搜不到"
> 这种查不出的间歇性 bug。横向扩之前把 `VECTOR_STORE=qdrant` 配好
> （`docker-compose.qdrant.yml`，向量变持久态、多 worker 共享），并设 `QDRANT_API_KEY`
> （Qdrant 默认无鉴权）。设了 `WEB_CONCURRENCY>1` 却仍是 memory 时，启动会打一条告警。
>
> 入库索引走持久队列（`document_jobs` 表 + 认领/租约/重试），所以多 worker 下任务不会
> 重复、进程重启不丢；取消「停止生成」也能跨 worker 生效（落库的 `cancelled` 状态，
> 循环在轮首顺带查一次）。

#### 前端部署

```bash
# 打包桌面应用
npm run package

# 或构建 Web 版本
npm run build
```

---

## ❓ 常见问题

### Q: 数据库连接失败?

**A**: 检查 MySQL 是否运行,确认 `.env` 中的连接字符串正确:

```bash
mysql -u root -p
# 验证数据库是否存在
SHOW DATABASES;
```

### Q: LLM API 调用失败?

**A**:

1. 检查 API Key 是否正确
2. 确认网络连接正常
3. 查看 API 配额是否用尽
4. 检查 Base URL 是否正确

### Q: 前端无法连接后端?

**A**:

1. 确认后端服务运行在 `http://localhost:3000`
2. 检查防火墙设置
3. 查看浏览器控制台错误信息

### Q: Electron 应用启动失败?

**A**:

1. 删除 `node_modules` 重新安装
2. 清除 Electron 缓存: `npm run clean`
3. 检查 Node.js 版本是否符合要求

---

## 🤝 贡献指南

欢迎贡献代码、报告问题或提出建议!

### 贡献流程

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

### 提交规范

```
feat: 新功能
fix: 修复 bug
docs: 文档更新
style: 代码格式调整
refactor: 重构
test: 测试相关
chore: 构建/工具链相关
```

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

感谢以下开源项目:

- [FastAPI](https://fastapi.tiangolo.com/)
- [React](https://react.dev/)
- [Electron](https://www.electronjs.org/)
- [Redux Toolkit](https://redux-toolkit.js.org/)
- [TailwindCSS](https://tailwindcss.com/)
- [Lucide Icons](https://lucide.dev/)

---

## 📧 联系方式

- 项目主页: [GitHub Repository](#)
- 问题反馈: [GitHub Issues](#)
- 讨论交流: [GitHub Discussions](#)

---

<div align="center">

**⭐ 如果这个项目对你有帮助,请给个星标支持一下! ⭐**

Made with ❤️ by AI Workspace Team

</div>
