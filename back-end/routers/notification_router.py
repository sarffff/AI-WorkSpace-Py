"""通知收件箱接口。

列出 / 数 / 标记已读**当前用户自己**的通知。全部按 current_user 自作用域（同
metrics_router / memory_router / audit_router 的约定）——通知本就是发给某个用户的，
别人看不到也标不了。

本轮只做应用内拉取；主动推送（IM/邮件/WebSocket）属外部基建、留作后续，见
models.Notification 的文档串。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import production_monitor
from services.notification_service import notification_service

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get("")
async def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户的通知，按时间倒序。"""
    return {
        "notifications": notification_service.list(
            db, current_user.id, unread_only=unread_only, limit=limit, offset=offset
        ),
        "unreadCount": notification_service.unread_count(db, current_user.id),
    }


@router.get("/unread_count")
async def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """未读数——给图标轨/顶栏的红点徽标用。

    顺带当线上健康监控的**心跳**：前端本就在轮询这个红点，于是不必引入调度器就能
    让告警"主动"起来。``evaluate_and_alert`` 自带节流（MONITOR_INTERVAL_MINUTES）与
    非阻塞兜底——绝大多数调用只是一次时间戳比较，关着（MONITOR_ENABLED=false）时
    直接空转。告警落给管理员，所以这里不分调用者是谁。
    """
    production_monitor.evaluate_and_alert(db)
    return {"count": notification_service.unread_count(db, current_user.id)}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = notification_service.mark_read(db, current_user.id, notification_id)
    if not ok:
        # 不存在或不属于当前用户——两者都回 404，不泄露"存在但不是你的"
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"ok": True}


@router.post("/read_all")
async def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"marked": notification_service.mark_all_read(db, current_user.id)}
