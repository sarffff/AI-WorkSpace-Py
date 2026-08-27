# 有据工作台 (YouJu Workspace)

<div align="center">

![有据工作台](https://img.shields.io/badge/有据-工作台-blue?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19.0-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5.5-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Electron](https://img.shields.io/badge/Electron-31.0-47848F?style=for-the-badge&logo=electron&logoColor=white)

**回答有据可查的企业知识库 AI 工作台**

集成知识库问答（RAG）、对话管理、提示词工程与全程可观测能力的桌面应用——每条引用可溯源、每次执行可回放、每条差评可变成回归用例。

[快速开始](#快速开始) · [功能特性](#功能特性) · [技术栈](#技术栈) · [开发文档](#开发文档)

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

### 🤖 AI 对话

- ✅ **多模型支持** - 支持 GLM-4.5、GPT、Claude 等主流大语言模型
- ✅ **流式响应** - 基于 SSE 的实时流式对话体验
- ✅ **会话管理** - 创建、重命名、删除、固定对话
- ✅ **历史记录** - 完整的对话历史持久化存储
- ✅ **上下文保持** - 自动维护对话上下文

### 📚 知识库 (RAG)

- ✅ **文档管理** - 上传、索引、管理知识库文档
- ✅ **向量检索** - 基于语义的智能检索
- ✅ **分块管理** - 文档自动分块和嵌入
- ✅ **状态追踪** - 实时查看文档处理状态

### 💡 提示词工程

- 🚧 提示词模板管理
- 🚧 提示词版本控制
- 🚧 提示词测试和优化

### ⚙️ 系统设置

- ✅ **模型配置** - 灵活切换不同的 AI 模型
- ✅ **数据库管理** - MySQL + Redis 双数据库支持
- ✅ **实时状态** - 服务器连接状态实时监控

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

#### 对话管理

```
GET    /chats                      # 获取所有对话
GET    /chats/{chat_id}/messages   # 获取对话消息
POST   /chats                      # 创建新对话
POST   /chats/completions          # 非流式对话
POST   /chats/completions/stream   # 流式对话 (SSE)
```

#### 知识库管理

```
GET    /knowledge/documents        # 获取文档列表
POST   /knowledge/documents        # 上传文档
DELETE /knowledge/documents/{id}  # 删除文档
```

### 请求示例

#### 创建对话

```bash
curl -X POST http://localhost:3000/chats \
  -H "Content-Type: application/json" \
  -d '{"title": "New Chat"}'
```

#### 发送消息 (流式)

```bash
curl -X POST http://localhost:3000/chats/completions/stream \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "你好,介绍一下你自己",
    "model": "glm-4.5-air",
    "chat_id": "chat_id_here"
  }'
```

---

## 🔧 开发指南

### 开发规范

- **代码风格**: 遵循 PEP 8 (Python) 和 ESLint (TypeScript)
- **提交规范**: 使用语义化提交信息
- **分支策略**: Git Flow

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
