import os

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from rate_limit import limiter

from config import settings
from database import init_db, SessionLocal
from models import Prompt
from routers import (
    chat_router,
    knowledge_router,
    auth_router,
    prompt_router,
    settings_router,
    attachment_router,
    metrics_router,
)

app = FastAPI(
    title="AI Workspace API",
    description="AI 助手桌面应用的后端 API",
    version="1.0.0",
    # 生产环境关闭文档接口
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ========== 安全响应头中间件 ==========
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS 白名单
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth_router.router)
app.include_router(chat_router.router)
app.include_router(knowledge_router.router)
app.include_router(prompt_router.router)
app.include_router(settings_router.router)
app.include_router(attachment_router.router)
app.include_router(metrics_router.router)

# 静态文件服务：附件上传后的访问入口
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


@app.get("/")
async def root():
    """API 根路径"""
    return {
        "message": "AI Workspace API",
        "version": "1.0.0",
        "status": "running"
    }


def _seed_prompts():
    """首次启动时写入内置提示词模板（若表中无数据）"""
    db = SessionLocal()
    try:
        if db.query(Prompt).count() == 0:
            seeds = [
                Prompt(
                    title="代码重构专家",
                    description="审查代码的性能、可读性与 TypeScript 最佳实践，并给出具体重构建议。",
                    category="Engineering",
                    content="你是一名资深的代码重构专家，精通 TypeScript/React/Python。请审查以下代码，从性能、可读性、类型安全、可维护性四个维度给出改进建议，并给出重构后的代码示例。\n\n用户输入：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="架构设计规划师",
                    description="设计可扩展的 monorepo 工作区与后端架构。",
                    category="Architecture",
                    content="你是一名系统架构师，擅长 monorepo（pnpm workspaces / Turborepo）与 NestJS/FastAPI 后端。请基于以下需求，给出模块划分、依赖关系、目录结构与关键接口设计。\n\n需求：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="SQL 查询优化器",
                    description="优化慢 SQL 查询并设计高效的 Prisma / SQLAlchemy 关系。",
                    category="Database",
                    content="你是数据库性能优化专家。请分析以下 SQL/ORM 查询，指出索引、连接、N+1 等问题，给出优化后的查询和必要的索引建议。\n\n查询：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="技术文档撰写助手",
                    description="把零散笔记整理为结构清晰的 API / 产品文档。",
                    category="Writing",
                    content="你是一名技术文档工程师。请把以下零散笔记整理成结构清晰的文档，包含概述、接口表、参数说明和示例。\n\n笔记：{input}",
                    is_public=True,
                ),
            ]
            db.add_all(seeds)
            db.commit()
            print(f"Seeded {len(seeds)} built-in prompts.")
    finally:
        db.close()


@app.on_event("startup")
async def startup():
    """应用启动时执行"""
    init_db()
    _seed_prompts()
    print(f"AI Workspace Server (Python) is running on: http://localhost:{settings.PORT}")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.PORT)
