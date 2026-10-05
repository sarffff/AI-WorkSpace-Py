"""工单轨迹：把每一步落库，供事后回放。

配合 ``models.TicketEvent``——为什么单独一张表、为什么 ticket_id 不设外键、为什么
存摘要而不是全文，见那个模型的文档串。

这里**只组装不吞异常**。"持久化失败不该拖垮主流程"是编排层的决定（它知道这一步丢了
要不要紧），不是轨迹层替所有调用方做的决定：把 try/except 写在这里，接入建档那种
"第一步就必须存在"的写入也会跟着静默失败，而那正是回放看到一条没有起点的轨迹的原因。
调用方要容错就自己包。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Ticket, TicketEvent
from services.approval_audit import digest as _digest
from services.approval_audit import preview as _preview
from services.clock import naive_now

# 状态机节点。与 services/ticket/graph.py 里的节点名一一对应，intake 是建单那一步
NODES = frozenset(
    {
        "intake",
        "understand",
        "risk",
        "retrieve",
        "plan",
        "act",
        "confirm",
        "escalate",
    }
)

# 步的类型。tool_call 与 tool_result 分成两条而不是合成一条：挂起在审批上的调用
# 有前者没有后者，合成一条就得给一个"永远不会有的结果"留空位
KINDS = frozenset(
    {
        "thinking",
        "tool_call",
        "tool_result",
        "approval",
        "decision",
        "state_change",
        "error",
        "reply",
    }
)

STATUSES = frozenset({"ok", "error", "blocked", "rejected", "pending"})


def next_seq(db: Session, ticket_id: str) -> int:
    """该工单内的下一个序号。

    取 max+1 而不是用数据库自增（同 ``audit_log.seq`` 的取舍）：BIGINT 自增在
    SQLite 与 MySQL 之间的行为差异不值得为它换掉"这是第几步"这个直观语义。
    """
    current = (
        db.query(func.max(TicketEvent.seq))
        .filter(TicketEvent.ticket_id == ticket_id)
        .scalar()
    )
    return int(current or 0) + 1


def append_event(
    db: Session,
    ticket: Ticket,
    *,
    node: str,
    kind: str,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    arguments: Any = None,
    result_excerpt: str | None = None,
    status: str | None = None,
    message: str | None = None,
    round_index: int | None = None,
    cost: Decimal | None = None,
) -> TicketEvent:
    """追加一步轨迹。**不 commit**——由调用方决定这一步和谁共享一个事务。

    ``arguments`` 走 ``approval_audit`` 的同一套摘要算法（sorted-json sha256 +
    截断 preview），于是"轨迹里的参数"和"审批记录里的参数摘要"是同一个 digest，
    可以直接对账证明批准的就是执行的。
    """
    if node not in NODES:
        raise ValueError(f"未知的轨迹节点：{node!r}，可选：{sorted(NODES)}")
    if kind not in KINDS:
        raise ValueError(f"未知的轨迹类型：{kind!r}，可选：{sorted(KINDS)}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"未知的轨迹状态：{status!r}，可选：{sorted(STATUSES)}")

    event = TicketEvent(
        id=str(uuid.uuid4()),
        ticket_id=ticket.id,
        workspace_id=ticket.workspace_id,
        seq=next_seq(db, ticket.id),
        node=node,
        kind=kind,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        args_digest=_digest(arguments) if arguments is not None else None,
        args_preview=_preview(arguments) if arguments is not None else None,
        result_excerpt=result_excerpt,
        status=status,
        message=message,
        round_index=round_index,
        cost=cost,
        created_at=naive_now(),
    )
    db.add(event)
    return event


def replay(db: Session, ticket_id: str, *, limit: int = 500) -> list[TicketEvent]:
    """按 seq 正序取整条轨迹。回放读的是顺序而不是时间戳：同一步里先写哪条
    是代码决定的，而 naive DATETIME 列没有亚秒精度可言。"""
    return (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.seq.asc())
        .limit(limit)
        .all()
    )
