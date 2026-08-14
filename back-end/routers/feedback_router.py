"""消息反馈接口。

线上反馈是评估集的原料来源:一次点踩加一句"应该说 X",就是一条现成的回归用例。
导出动作放在 eval 侧的脚本里(``python -m eval.from_feedback``),这里只负责收集。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services.feedback_service import RATINGS, REASONS, feedback_service

router = APIRouter(prefix="/feedback", tags=["反馈"])


class FeedbackCreate(BaseModel):
    messageId: str
    rating: str
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=2000)
    expectedAnswer: str | None = Field(default=None, max_length=4000)


@router.post("")
async def submit_feedback(
    body: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.rating not in RATINGS:
        raise HTTPException(status_code=400, detail=f"rating 必须是 {RATINGS} 之一")
    if body.reason is not None and body.reason not in REASONS:
        raise HTTPException(status_code=400, detail=f"reason 必须是 {REASONS} 之一")

    feedback = feedback_service.submit(
        db,
        current_user.id,
        body.messageId,
        body.rating,
        reason=body.reason,
        comment=body.comment,
        expected_answer=body.expectedAnswer,
    )
    if feedback is None:
        # 消息不存在、不是助手消息，或不属于当前用户——都不透露具体是哪一种
        raise HTTPException(status_code=404, detail="未找到可反馈的消息")
    return {
        "messageId": feedback.message_id,
        "rating": feedback.rating,
        "reason": feedback.reason,
        "comment": feedback.comment,
        "expectedAnswer": feedback.expected_answer,
    }


@router.delete("/{message_id}")
async def revoke_feedback(
    message_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    removed = feedback_service.revoke(db, current_user.id, message_id)
    return {"success": removed}


@router.get("")
async def list_feedback(
    messageIds: str = Query(default="", description="逗号分隔的消息 id"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ids = [part for part in (messageIds or "").split(",") if part]
    return {"items": list(feedback_service.for_messages(db, current_user.id, ids).values())}


@router.get("/summary")
async def feedback_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return feedback_service.summary(db, current_user.id)
