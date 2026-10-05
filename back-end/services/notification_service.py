"""应用内通知：持久化的 per-user "有事等你" 收件箱。

配合 models.Notification——为什么需要它、边界在哪，见该模型的文档串。

两条和 audit_log / approval_audit 一致的取舍：
- **写入失败只记日志、不抛。** 通知是旁路记录，绝不能让一次通知写失败把审批/中断
  主流程也带崩（通知是在那些流程的发射点顺带创建的）。
- **去重防刷屏。** 中断恢复会重入同一个 waiting_* 状态，同一张工单可能多次走到发射
  点。给了 ticket_id 时，若已有同 (user, kind, ticket) 的**未读**通知就跳过——已读之后
  再来才是真的新事件。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from models import Notification
from services.clock import naive_now

logger = logging.getLogger("notification")


class NotificationService:
    def create(
        self,
        db: Session,
        *,
        user_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        ticket_id: str | None = None,
    ) -> str | None:
        """创建一条通知。返回 id；失败或被去重跳过时返回 None（只记日志、不抛）。"""
        try:
            if ticket_id is not None:
                existing = (
                    db.query(Notification.id)
                    .filter(
                        Notification.user_id == user_id,
                        Notification.kind == kind,
                        Notification.ticket_id == ticket_id,
                        Notification.read_at.is_(None),
                    )
                    .first()
                )
                if existing is not None:
                    return None  # 同一件事已有未读通知，别刷屏
            note = Notification(
                id=str(uuid.uuid4()),
                user_id=user_id,
                kind=kind,
                title=title[:255],
                body=body,
                ticket_id=ticket_id,
                created_at=naive_now(),
            )
            db.add(note)
            db.commit()
            return note.id
        except Exception:
            db.rollback()
            logger.exception(
                "failed to create notification kind=%s user=%s", kind, user_id
            )
            return None

    def list(
        self,
        db: Session,
        user_id: str,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """某用户的通知，按时间倒序（新的在前）。自作用域。"""
        query = db.query(Notification).filter(Notification.user_id == user_id)
        if unread_only:
            query = query.filter(Notification.read_at.is_(None))
        rows = (
            query.order_by(Notification.created_at.desc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
            .all()
        )
        return [self._to_dict(row) for row in rows]

    def unread_count(self, db: Session, user_id: str) -> int:
        return (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
            .count()
        )

    def mark_read(self, db: Session, user_id: str, notification_id: str) -> bool:
        """标记一条已读。按 user_id 自作用域——改不动别人的通知。"""
        note = (
            db.query(Notification)
            .filter(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            .first()
        )
        if note is None:
            return False
        if note.read_at is None:
            note.read_at = naive_now()
            db.commit()
        return True

    def mark_all_read(self, db: Session, user_id: str) -> int:
        updated = (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
            .update({Notification.read_at: naive_now()}, synchronize_session=False)
        )
        db.commit()
        return int(updated)

    @staticmethod
    def _to_dict(row: Notification) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "body": row.body,
            "ticketId": row.ticket_id,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "readAt": row.read_at.isoformat() if row.read_at else None,
            "read": row.read_at is not None,
        }


notification_service = NotificationService()
