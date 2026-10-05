"""SLA 超时：到点还没有结论的工单，转人工并**带上上下文**。

对应文档工作流的第 8 步"异常/超时处理 → 转人工并带上上下文"。那句"带上上下文"
是这一层的重点：把工单标成 escalated 只是在队列里挪了个颜色，接手的人真正需要的是
"之前查到过什么、卡在哪一步"，那已经在 ``ticket_events`` 里，抽一段递给他就行。

为什么挂在读路径上而不是定时器：这个仓库里没有调度器，``agent_runs`` 的租约回收、
``document_jobs`` 的重试、审批过期清理全都挂在读路径上（见
``approval_audit.expire_stale`` 与 ``document_queue``）。同一个形状意味着
同一种运维性质——不需要额外进程，也不会出现"定时器停了所以从来没人管"。
代价是没人打开工单台时超时不会被处理，而那种时刻恰恰没有人被卡住。

超时判定读的是 ``sla_due_at`` 这一**建单时**写下的时刻，不是"现在减去创建时间"。
理由是文档把"平均处理时长"和 SLA 都列为指标：用一个今天的标准去倒推历史的
工单有没有超期，等于用现在的口径审判过去，那个数就没法和当时人对上。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import Ticket, TicketEvent
from services.clock import naive_now
from services.ticket.trace import append_event

# 还没走出 Agent 手里的那些状态。``awaiting_approval`` 刻意不在这里：那张单
# 已经交到人面前了，再判一次"超时转人工"会把正在等人点头的工单抢走，
# 而审批收件箱里它有自己的时效呈现
OPEN_STATUSES = ("new", "understanding", "planning", "acting")

TERMINAL_STATUSES = ("resolved", "closed", "escalated", "failed")

# 交接时递给接手人的上下文长度。三步是"看得出它走到哪儿了"又不至于把
# 整个列表页变成轨迹查看器的量
_HANDOFF_STEPS = 3


def context_for_handoff(db: Session, ticket: Ticket, *, limit: int = _HANDOFF_STEPS) -> str:
    """最后几步轨迹，拼成一句能读的话。"""
    rows = (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket.id)
        .order_by(TicketEvent.seq.desc())
        .limit(max(1, limit))
        .all()
    )
    if not rows:
        return "还没有任何执行痕迹"
    parts = []
    for row in reversed(rows):
        detail = row.message or row.result_excerpt or row.status or ""
        label = f"{row.node}/{row.kind}"
        parts.append(f"{label}：{str(detail)[:160]}")
    return "；".join(parts)


def overdue_query(db: Session, *, at: datetime | None = None, workspace_id: str | None = None):
    moment = at or naive_now()
    query = db.query(Ticket).filter(
        Ticket.sla_due_at.is_not(None),
        Ticket.sla_due_at <= moment,
        Ticket.status.in_(OPEN_STATUSES),
    )
    if workspace_id:
        query = query.filter(Ticket.workspace_id == workspace_id)
    return query


def reap_overdue(
    db: Session, *, at: datetime | None = None, limit: int = 50, workspace_id: str | None = None
) -> list[Ticket]:
    """把超时未办结的工单转人工。返回被转走的工单。

    **只标状态、不取消正在跑的那次执行。** 一条工单可能正被模型驱动着，
    这边判超时而那边还在推进：把状态改成 escalated 之后驱动方最终会走到
    confirm/escalate 并覆盖它，那看起来像"抖动"，实际后果只是接手的人看到
    的状态晚了一步更新——比强杀一次已经花了钱的执行好。真要取消，走
    ``services/cancellation``，那是另一条路径。
    """
    rows = (
        overdue_query(db, at=at, workspace_id=workspace_id)
        .order_by(Ticket.sla_due_at.asc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    reaped: list[Ticket] = []
    for ticket in rows:
        ticket.status = "escalated"
        ticket.escalation_reason = "sla_overdue"
        ticket.updated_at = naive_now()
        append_event(
            db,
            ticket,
            node="escalate",
            kind="decision",
            status="pending",
            message=(
                f"超过 SLA（{ticket.sla_due_at:%Y-%m-%d %H:%M}）仍未办结，自动转人工。"
                f"当前进展——{context_for_handoff(db, ticket)}"
            ),
        )
        reaped.append(ticket)
    if reaped:
        db.commit()
        # 超时会自己发生，但不会自己被人发现。每一条都要有一个收件箱里的条目，
        # 否则这条能力的实际效果是"队列里多了几张红色的单"
        from services.ticket import alerts

        for ticket in reaped:
            alerts.notify_handoff(db, ticket, "sla_overdue")
    return reaped


def is_overdue(ticket: Ticket, *, at: datetime | None = None) -> bool:
    moment = at or naive_now()
    return bool(
        ticket.sla_due_at
        and ticket.sla_due_at <= moment
        and ticket.status in OPEN_STATUSES
    )


def summarize_overdue(
    db: Session, *, workspace_id: str, at: datetime | None = None
) -> dict[str, Any]:
    """队列页顶部那条"有多少张已经超时"。只数，不改状态。"""
    rows = overdue_query(db, at=at, workspace_id=workspace_id).all()
    return {
        "overdue": len(rows),
        "ticketIds": [row.id for row in rows[:20]],
    }
