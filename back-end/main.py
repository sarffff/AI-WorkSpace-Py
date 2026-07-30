import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import init_db
from routers import chat_router, knowledge_router

app = FastAPI(title="AI Workspace Server (Python)")

# CORS 配置（对应 NestJS 的 app.enableCors()）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由（对应 NestJS 的 Module imports）
app.include_router(chat_router.router)
app.include_router(knowledge_router.router)


@app.on_event("startup")
async def startup():
    """应用启动时执行（对应 NestJS 的 onModuleInit）"""
    init_db()
    print(f"AI Workspace Server (Python) is running on: http://localhost:{settings.PORT}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
