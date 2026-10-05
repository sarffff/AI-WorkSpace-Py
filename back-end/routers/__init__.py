"""路由模块导入。

只装工单 Agent 这条链路上的接口：接入与治理（ticket）、政策知识库（knowledge）、
作业指导（skill）、附件（attachment）、账号与工作区（auth/workspace）、观测
（metrics/notification/audit）。
"""
from . import (
    attachment_router,
    audit_router,
    auth_router,
    knowledge_router,
    metrics_router,
    notification_router,
    skill_router,
    ticket_router,
    workspace_router,
)

__all__ = [
    "attachment_router",
    "audit_router",
    "auth_router",
    "knowledge_router",
    "metrics_router",
    "notification_router",
    "skill_router",
    "ticket_router",
    "workspace_router",
]
