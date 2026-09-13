"""审核台账：看结论、处置待办。

## 为什么台账要有自己的入口

审核类任务的交付物是结论，而结论在对话文本里是拿不走的。这组接口存在的理由是那
三件事：**拿得走**（导出给下游）、**复核得了**（依据都在，不用重读整件事）、
**追溯得到**（当时按的哪一版 SOP）。

## 待办筛选不是便利功能

``needs_human`` 那一档如果没有"待办在哪"的入口，转人工就等于把结论扔进一个没人看
的队列——而那比直接给一个错结论更难发现：错结论至少会被人读到，队列里的东西
连读都不会被读。所以 ``pending_only`` 是这组接口存在的一半理由。

## 谁能看、谁能处置

看：工作区全员。他们需要知道 agent 按什么规程判了什么——这和知识库共享文档同一个
级别的可见性。

处置：也是全员，**不限 admin**。这一条和"改 SOP 只有 admin 能做"刻意不同：
定规矩是管理行为，而按规矩复核一张单子是日常工作，把它锁给 admin 会让待办队列
堵在一个人身上，而那正好是转人工要避免的形状。谁处置的记在 ``resolved_by`` 里。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import User
from services import review_service

router = APIRouter(prefix="/reviews", tags=["审核台账"])


class ResolveRequest(BaseModel):
    # approved / rejected / amended。校验在 service 层，那里的错误消息可读
    resolution: str = Field(min_length=1, max_length=20)
    # 1000 与 review_verdicts.resolution_note 的列宽对齐：在这里 422 比入库时
    # 静默截断好——截断会把"因为 X 所以批了"截成"因为"
    note: str = Field(default="", max_length=1000)


def _require_workspace(user: User) -> str:
    if not user.workspace_id:
        raise HTTPException(status_code=400, detail="当前用户还没有工作区")
    return user.workspace_id


@router.get("")
async def list_reviews(
    pending_only: bool = Query(
        False, description="只看还等着人判断的（needs_human 且没人处置过）"
    ),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """台账，按时间倒序。

    ``enabled`` 一起返回：关着开关时台账是空的，而"没开这个功能"和"还没审过任何
    东西"在界面上长得一样。不说清的话用户会以为审核记录丢了。
    """
    workspace_id = _require_workspace(current_user)
    return {
        "items": review_service.list_for_workspace(
            db, workspace_id, pending_only=pending_only, limit=limit
        ),
        "enabled": settings.REVIEW_LEDGER_ENABLED,
    }


@router.post("/{verdict_id}/resolve")
async def resolve_review(
    verdict_id: str,
    body: ResolveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """记下人复核之后的处置。

    错误一律 400，不区分"不存在"与"不属于你的工作区"：那个区别本身就是信息
    （同 ``fs_router`` 里那条理由）。
    """
    workspace_id = _require_workspace(current_user)
    try:
        return review_service.resolve(
            db,
            workspace_id,
            verdict_id,
            user_id=current_user.id,
            resolution=body.resolution,
            note=body.note,
        )
    except review_service.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
