"""写操作账本：幂等的落点、当日额度的聚合源、审计的原始材料。

配合 ``models.CsOperation``——那张表回答的三个问题（做过没有 / 今天用了多少 /
是谁按什么参数做的）见其文档串。

幂等键的来源要说明一下：**模型不需要自己想一个键**。``derive_key`` 由
``(工单, 操作, 归一化后的参数)`` 算出来，同一个工单里对同一笔订单发起同样金额的
退款，天然就是同一个键。让模型编键意味着它会编出一个每次都不同的随机串，
而那种键的幂等性是零——看起来有这一列，实际上什么都挡不住。
显式传入的键优先，那是给"人改过参数之后重新发起"这种真实场合物色保留的口子。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import CsOperation
from services.approval_audit import digest as _digest
from services.approval_audit import preview as _preview
from services.clock import naive_now

# pending_approval / blocked 这些状态是编排层与治理层写的，工具层只写
# executed / replayed / failed
STATUSES = ("executed", "replayed", "blocked", "failed", "pending_approval")

# 人工复盘的结论。只有两个值是有原因的：复盘要回答的是"这一笔该不该做"，
# 而不是"做得漂不漂亮"——中间档位会让标注人把"能过但勉强"和"不该过"混着点，
# 那之后算出来的错误操作率就没法用来决定要不要收紧阈值。
VERDICTS = ("ok", "wrong")

# 值得复盘的状态：真的改变了业务状态的那些。blocked/failed 有自己的状态在说话
REVIEWABLE_STATUSES = ("executed", "replayed")


class LedgerError(ValueError):
    """账本操作不成立。router 一律映射成 400。"""


def get(db: Session, workspace_id: str, operation_id: str) -> CsOperation | None:
    """按工作区取一笔账。跨区取不到——返回 None 而不是抛，让调用方按"不存在"处理。

    刻意不区分"不存在"与"不是你的"：这条边界一旦泄露，就能用状态码枚举出
    别人工作区里有哪些操作 id。
    """
    return (
        db.query(CsOperation)
        .filter(CsOperation.workspace_id == workspace_id, CsOperation.id == operation_id)
        .first()
    )


def record_review(
    db: Session,
    workspace_id: str,
    operation_id: str,
    *,
    reviewer_id: str,
    verdict: str,
    action: str | None = None,
    note: str | None = None,
) -> CsOperation:
    """给一笔已执行的操作记复盘结论。

    重复标注允许（人以最后一次为准），但每次都会刷新 ``corrected_at``——
    "先判错后改判对"这件事本身是阈值收紧讨论里最有信息量的一条。
    """
    if verdict not in VERDICTS:
        raise LedgerError(f"未知的复盘结论：{verdict!r}，可选：{list(VERDICTS)}")
    row = get(db, workspace_id, operation_id)
    if row is None:
        raise LedgerError("找不到这笔操作。")
    if row.status not in REVIEWABLE_STATUSES:
        raise LedgerError(
            f"状态为 {row.status} 的操作不需要复盘——它没有真的改变业务状态，"
            "把它计入错误操作率只会让指标随拦截量抖动。"
        )
    row.reviewed_by = reviewer_id
    row.verdict = verdict
    row.corrected_at = naive_now()
    row.corrected_action = (action or "").strip()[:40] or None
    row.review_note = (note or "").strip()[:500] or None
    db.commit()
    return row


def unreviewed(
    db: Session, workspace_id: str, *, since=None, limit: int = 50
) -> list[CsOperation]:
    """待复盘队列：执行过、还没人标注的那些。

    没有这个队列，错误操作率就永远算不出来——指标需要一个**入口**才会被填，
    光有列没人标注等于没有那一列。
    """
    query = db.query(CsOperation).filter(
        CsOperation.workspace_id == workspace_id,
        CsOperation.status.in_(REVIEWABLE_STATUSES),
        CsOperation.verdict.is_(None),
    )
    if since is not None:
        query = query.filter(CsOperation.created_at >= since)
    return query.order_by(CsOperation.created_at.desc()).limit(max(1, min(limit, 200))).all()


def derive_key(
    *, ticket_id: str | None, tool_name: str, operation: str, arguments: dict[str, Any]
) -> str:
    """从"这张工单、这个操作、这份参数"推出幂等键。

    参数按 key 排序后序列化（同 ``RepeatGuard.key``）：``{"a":1,"b":2}`` 与
    ``{"b":2,"a":1}`` 必须得出同一个键，否则模型换个字段顺序就说这不是同一笔退款。
    ``idempotency_key`` 本身不参与推导——那是调用方传进来的覆盖项，把它算进去
    会让每次显式传键都得到一个不同的键。
    """
    payload = {
        name: value
        for name, value in sorted((arguments or {}).items())
        if name != "idempotency_key"
    }
    encoded = json.dumps(
        {"ticket": ticket_id, "operation": operation, "arguments": payload},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:64]


def find(db: Session, workspace_id: str, idempotency_key: str) -> CsOperation | None:
    return (
        db.query(CsOperation)
        .filter(
            CsOperation.workspace_id == workspace_id,
            CsOperation.idempotency_key == idempotency_key,
        )
        .first()
    )


def open_row(
    db: Session,
    *,
    workspace_id: str,
    ticket_id: str | None,
    tool_name: str,
    operation: str,
    permission: str,
    idempotency_key: str,
    arguments: dict[str, Any],
    status: str,
    amount: Decimal | None = None,
    currency: str | None = None,
    actor: str | None = None,
    blocked_reason: str | None = None,
) -> CsOperation:
    """记一笔账。**不 commit**：这一行必须和被改的业务行落在同一个事务里，
    否则会出现"账上记了退款，订单上没退"或者反过来。"""
    if status not in STATUSES:
        raise ValueError(f"未知的账本状态：{status!r}，可选：{list(STATUSES)}")
    row = CsOperation(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        ticket_id=ticket_id,
        tool_name=tool_name,
        operation=operation,
        permission=permission,
        idempotency_key=idempotency_key,
        args_digest=_digest(arguments),
        args_preview=_preview(arguments),
        status=status,
        blocked_reason=blocked_reason,
        amount=amount,
        currency=currency,
        actor=actor,
        created_at=naive_now(),
        completed_at=naive_now() if status in ("executed", "failed", "blocked") else None,
    )
    db.add(row)
    return row


def finish(
    db: Session,
    row: CsOperation,
    *,
    status: str,
    result_excerpt: str | None = None,
    blocked_reason: str | None = None,
) -> CsOperation:
    if status not in STATUSES:
        raise ValueError(f"未知的账本状态：{status!r}，可选：{list(STATUSES)}")
    row.status = status
    row.result_excerpt = result_excerpt
    row.blocked_reason = blocked_reason
    row.completed_at = naive_now()
    return row


def commit_or_replay(db: Session, row: CsOperation) -> CsOperation:
    """把这笔账落到库上，返回**权威的那一行**。

    返回的不是传进来的 ``row`` 就说明这次是重复调用（撞上了唯一约束），调用方应当
    把返回行里的首次结果答复出去，而不是把自己刚做的业务改动留在库里。

    撞键时整个事务会被 rollback——这是这里唯一正确的动作：账本行和业务行必须在
    同一个事务里，否则就会留下"订单已经退了款而账上查不到是谁退的"那种状态。
    代价是同一次会话里先前攒下的改动也一起回滚，所以调用方要在每个节点结束时
    提交一次（编排层就是这么做的），而真正的并发撞键在单进程的工单处理里是罕见路径：
    常见路径由 ``find`` 在动手之前就挡住了，这里只是那条兜底的第二道。
    """
    try:
        db.flush()
        return row
    except IntegrityError:
        db.rollback()
        winner = find(db, row.workspace_id, row.idempotency_key or "")
        if winner is None:
            # 唯一约束被撞却查不到那行：不可能凭空发生，多半是连接或库出了问题。
            # 静默返回一个假的首次结果比抛错更糟，因为调用方会据此答复客户"已办理"。
            raise
        return winner
