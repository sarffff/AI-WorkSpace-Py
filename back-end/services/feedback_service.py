"""消息级反馈的读写。

这一层的价值不在"存个赞踩",而在让线上出现的坏回答能直接变成离线回归用例——
否则每次改提示词或检索配置都只能凭感觉判断有没有变好。

两个刻意的设计:
- 一条消息只保留一份反馈,用 upsert 而不是追加。同一个用户反复点踩不该被算成
  多个负样本。
- 反馈里存用户文本(补充说明、期望答案)。这和埋点"只存元数据"的约定不冲突:
  埋点是系统自动采集的,反馈是用户主动提交的标注,后者本来就是要拿去用的。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Chat, Message, MessageFeedback
from services.clock import naive_now

logger = logging.getLogger("feedback_service")

RATINGS = ("up", "down")
REASONS = ("inaccurate", "no_citation", "off_topic", "bad_format", "other")


class FeedbackService:
    @staticmethod
    def _question_for(db: Session, message: Message) -> str | None:
        """找到这条回答对应的用户提问,导出回归用例时要用。"""
        previous = (
            db.query(Message)
            .filter(
                Message.chat_id == message.chat_id,
                Message.role == "user",
                Message.created_at <= message.created_at,
                Message.id != message.id,
            )
            .order_by(Message.created_at.desc(), Message.seq.desc())
            .first()
        )
        return previous.content if previous else None

    def submit(
        self,
        db: Session,
        user_id: str,
        message_id: str,
        rating: str,
        reason: str | None = None,
        comment: str | None = None,
        expected_answer: str | None = None,
    ) -> MessageFeedback | None:
        """写入或更新一条反馈。消息不存在或不属于该用户时返回 None。"""
        message = db.query(Message).filter(Message.id == message_id).first()
        if not message or message.role != "assistant":
            return None
        chat = db.query(Chat).filter(Chat.id == message.chat_id).first()
        if not chat or chat.user_id != user_id:
            return None

        now = naive_now()
        existing = (
            db.query(MessageFeedback)
            .filter(MessageFeedback.message_id == message_id)
            .first()
        )
        if existing:
            existing.rating = rating
            existing.reason = reason
            existing.comment = comment
            existing.expected_answer = expected_answer
            existing.updated_at = now
            # 内容改过就要重新导出,否则评估集里留的是旧版标注
            existing.exported_at = None
            db.commit()
            db.refresh(existing)
            return existing

        feedback = MessageFeedback(
            message_id=message_id,
            chat_id=message.chat_id,
            user_id=user_id,
            rating=rating,
            reason=reason,
            comment=comment,
            expected_answer=expected_answer,
            model=message.model,
            question=self._question_for(db, message),
            created_at=now,
            updated_at=now,
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)
        return feedback

    @staticmethod
    def revoke(db: Session, user_id: str, message_id: str) -> bool:
        """撤销反馈(再次点击同一个按钮)。"""
        feedback = (
            db.query(MessageFeedback)
            .filter(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user_id,
            )
            .first()
        )
        if not feedback:
            return False
        db.delete(feedback)
        db.commit()
        return True

    @staticmethod
    def discard_chat(db: Session, chat_id: str) -> int:
        """删对话时一并删掉它的反馈。

        这里的策略要说清楚,因为两种做法都能自圆其说:

        - **随对话删**(现在的选择)。用户明确要求删掉这段对话,库里不该继续留着
          它的提问原文和用户批注。已经导出过的那些内容在
          ``eval/datasets/feedback_regression.jsonl`` 里,那个文件才是回归数据集
          的真实载体,删这一行不会让评估资产消失;而 ``exported_at`` 只是去重标记,
          行没了也就不会被重复导出。
        - 保留。好处是"用户删了对话但反馈还在"可以继续统计满意度,代价是删除
          操作没有真正删掉用户文本。单用户的本地工作台里,后者的代价更大。

        「编辑并重新生成」不走这条路:那种情况下反馈行本身是自洽的
        (question / model / expected_answer 都存了),依然是一条有效的回归用例,
        而用户也没有表达"删掉这段"。
        """
        return (
            db.query(MessageFeedback)
            .filter(MessageFeedback.chat_id == chat_id)
            .delete(synchronize_session=False)
        )

    @staticmethod
    def for_messages(
        db: Session, user_id: str, message_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """批量取回反馈,前端一次请求就能把整个会话的赞踩状态点亮。"""
        if not message_ids:
            return {}
        rows = (
            db.query(MessageFeedback)
            .filter(
                MessageFeedback.user_id == user_id,
                MessageFeedback.message_id.in_(message_ids),
            )
            .all()
        )
        return {
            row.message_id: {
                "messageId": row.message_id,
                "rating": row.rating,
                "reason": row.reason,
                "comment": row.comment,
                "expectedAnswer": row.expected_answer,
            }
            for row in rows
        }

    @staticmethod
    def summary(db: Session, user_id: str) -> dict[str, Any]:
        """满意度概览。分母只算"有反馈的回答",不是所有回答。"""
        rows = (
            db.query(MessageFeedback.rating, func.count(MessageFeedback.id))
            .filter(MessageFeedback.user_id == user_id)
            .group_by(MessageFeedback.rating)
            .all()
        )
        counts = {rating: int(total) for rating, total in rows}
        up = counts.get("up", 0)
        down = counts.get("down", 0)
        rated = up + down

        reasons = (
            db.query(MessageFeedback.reason, func.count(MessageFeedback.id))
            .filter(
                MessageFeedback.user_id == user_id,
                MessageFeedback.rating == "down",
                MessageFeedback.reason.isnot(None),
            )
            .group_by(MessageFeedback.reason)
            .all()
        )
        pending = (
            db.query(func.count(MessageFeedback.id))
            .filter(
                MessageFeedback.user_id == user_id,
                MessageFeedback.rating == "down",
                MessageFeedback.exported_at.is_(None),
            )
            .scalar()
        )
        return {
            "up": up,
            "down": down,
            "rated": rated,
            # 没有任何反馈时给 None 而不是 0：区分"没人评价"和"评价全是差评"
            "satisfaction": (up / rated) if rated else None,
            "downReasons": [
                {"reason": reason, "count": int(total)} for reason, total in reasons
            ],
            "pendingExport": int(pending or 0),
        }


feedback_service = FeedbackService()
