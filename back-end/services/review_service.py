"""审核结论的落库与读取。

## 为什么写库这一步单独一个模块

``review_consensus`` 是纯逻辑（合并 N 份结论），不碰数据库；这里只管持久化。
分开的理由和 ``approval`` / ``approval_audit`` 那一对相同：前者是可以随便调的
纯函数，后者需要 Session。混在一起会让合并逻辑变成"必须有库才能测"的东西。

## 写入失败要不要让工具失败

**要。** 这一条和 ``approval_audit``（写审计失败只记日志）相反，值得说清为什么：

审计是对"已经发生的事"的记录，丢一条不改变那件事的效力。而审核结论**本身就是
交付物**——落库失败意味着这次审核没有产出，那时告诉模型"记下了"是在骗它，
它会照着向用户复述"已归档"。

所以这里的失败必须冒泡，让模型知道没记上、由它告诉用户。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ReviewRecord
from services.clock import naive_now
from services.review_consensus import ConsensusResult

logger = logging.getLogger("review_service")

# 人复核之后的处置。和模型给的 verdict 分开存：
# "模型说需要人判断，人看完批了" 与 "模型直接说通过" 是两件不同的事，
# 而混成一列之后就分不出来了——那正好丢掉了转人工这条链的全部信息
RESOLUTIONS = ("approved", "rejected", "amended")


class ReviewError(Exception):
    """业务错误，由路由层转成 4xx。message 面向最终用户。"""


def record(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    subject: str,
    result: ConsensusResult,
    chat_id: str | None = None,
    message_id: str | None = None,
) -> ReviewRecord:
    """把一次审核的结论写进台账。

    ``subject`` 是"审的是什么"，由调用方给：它可能是文件名、单号或一句描述，
    取决于材料从哪来。空的话拒绝写入——一条查不出对象的结论在台账上等于噪声。
    """
    cleaned = (subject or "").strip()
    if not cleaned:
        raise ReviewError("subject 不能为空：台账上要能看出这条结论审的是什么。")

    verdict = result.verdict
    row = ReviewRecord(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        subject=cleaned[:500],
        sop_name=verdict.sop_name[:80],
        sop_version=verdict.sop_version,
        verdict=verdict.verdict,
        # 存 JSON 字符串而不是依赖数据库的 JSON 类型：MySQL 与 SQLite 的 JSON
        # 支持不一致，而这两列只整体读写、从不按内部字段查询
        inputs=json.dumps(
            [item.model_dump() for item in verdict.inputs], ensure_ascii=False
        ),
        basis=json.dumps(verdict.basis, ensure_ascii=False),
        runs=result.runs,
        agreed=result.agreed,
        created_at=naive_now(),
    )
    db.add(row)
    # 不吞异常（与 approval_audit 相反，理由见模块文档）：结论就是交付物，
    # 落库失败意味着这次审核没有产出
    db.commit()
    db.refresh(row)
    return row


def _to_dict(row: ReviewRecord) -> dict[str, Any]:
    """给界面看的一条结论。

    ``inputs`` / ``basis`` 解析失败时给空列表而不是抛：一条 JSON 坏了的历史记录
    不该让整个台账页面打不开。坏在哪由日志说。
    """

    def _load(raw: str, field: str) -> list[Any]:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            logger.warning("review %s has unreadable %s", row.id, field)
            return []
        return value if isinstance(value, list) else []

    return {
        "id": row.id,
        "subject": row.subject,
        "sopName": row.sop_name,
        "sopVersion": row.sop_version,
        "verdict": row.verdict,
        "inputs": _load(row.inputs, "inputs"),
        "basis": _load(row.basis, "basis"),
        "runs": row.runs,
        # runs=1 且 agreed=True 的含义是"没检查过"，不是"检查过并且一致"。
        # 界面要分开显示，所以两个字段都给，不合成一个布尔
        "agreed": row.agreed,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "chatId": row.chat_id,
        "resolvedBy": row.resolved_by,
        "resolvedAt": row.resolved_at.isoformat() if row.resolved_at else None,
        "resolution": row.resolution,
        "resolutionNote": row.resolution_note,
    }


def list_for_workspace(
    db: Session,
    workspace_id: str,
    *,
    pending_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """台账。按时间倒序。

    ``pending_only`` 取的是"还等着人看"——``needs_human`` 且没人处置过。
    这个筛选存在的理由：转人工如果没有一个"待办在哪"的入口，它就等于把结论
    扔进一个没人看的队列，而那比直接给个错结论更难发现。
    """
    stmt = select(ReviewRecord).where(ReviewRecord.workspace_id == workspace_id)
    if pending_only:
        stmt = stmt.where(
            ReviewRecord.verdict == "needs_human",
            ReviewRecord.resolved_at.is_(None),
        )
    stmt = stmt.order_by(ReviewRecord.created_at.desc()).limit(
        max(1, min(limit, 200))
    )
    return [_to_dict(row) for row in db.execute(stmt).scalars()]


def resolve(
    db: Session,
    workspace_id: str,
    verdict_id: str,
    *,
    user_id: str,
    resolution: str,
    note: str = "",
) -> dict[str, Any]:
    """人复核之后记下处置。

    判据用 ``workspace_id`` 过滤而不是只按 id 查：台账是组织资产，但不能让
    A 工作区的人处置 B 工作区的结论。找不到时的错误消息不区分"不存在"与
    "不属于你"——那个区别本身就是信息（同 ``fs_router`` 里一律 400 的理由）。
    """
    if resolution not in RESOLUTIONS:
        raise ReviewError(
            f"resolution 只能是 {'、'.join(RESOLUTIONS)} 之一。"
        )
    row = (
        db.query(ReviewRecord)
        .filter(
            ReviewRecord.id == verdict_id,
            ReviewRecord.workspace_id == workspace_id,
        )
        .first()
    )
    if row is None:
        raise ReviewError("找不到这条审核结论。")
    # 已经处置过的不允许改：台账要能作为依据，而一条可以反复改写的记录
    # 作不了依据。要更正就再审一次，留下新的一条
    if row.resolved_at is not None:
        raise ReviewError(
            "这条结论已经有人处置过了。需要更正请重新审一次，留下新的记录。"
        )
    row.resolved_by = user_id
    row.resolved_at = naive_now()
    row.resolution = resolution
    row.resolution_note = (note or "").strip()[:1000] or None
    db.commit()
    db.refresh(row)
    return _to_dict(row)


__all__ = [
    "RESOLUTIONS",
    "ReviewError",
    "list_for_workspace",
    "record",
    "resolve",
]
