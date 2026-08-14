"""路由模块导出"""
from . import (
    auth_router,
    chat_router,
    knowledge_router,
    prompt_router,
    settings_router,
    attachment_router,
    metrics_router,
    feedback_router,
)

__all__ = [
    "auth_router",
    "chat_router",
    "knowledge_router",
    "prompt_router",
    "settings_router",
    "attachment_router",
    "metrics_router",
    "feedback_router",
]
