"""SLA 超时处理：谁被转走、谁不该被动，以及交接时递出去的那段上下文。"""
from datetime import timedelta

import pytest

from config import settings
from models import Ticket, TicketEvent
from services.clock import naive_now
from services.ticket import sla
from services.ticket.intake import TicketIntake, submit_ticket

WS = "w1"


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat,email,app,api")
    monkeypatch.setattr(settings, "TICKET_SLA_HOURS", 24)


def _ticket(db, *, hours_ago=30, status="new", with_trace=True, sla_hours=24) -> Ticket:
    now = naive_now()
    ticket = Ticket(
        id=f"t-{hours_ago}-{status}",
        workspace_id=WS,
        channel="web_chat",
        status=status,
        request_text="订单 ORD-1 还没发货",
        created_at=now - timedelta(hours=hours_ago),
        updated_at=now,
        sla_due_at=now - timedelta(hours=hours_ago - sla_hours) if sla_hours else None,
    )
    db.add(ticket)
    db.commit()
    if with_trace:
        db.add(
            TicketEvent(
                id=f"e-{ticket.id}",
                ticket_id=ticket.id,
                workspace_id=WS,
                seq=1,
                node="act",
                kind="tool_result",
                status="ok",
                tool_name="lookup_order",
                message="查到这单还是 paid，没发货",
                created_at=now,
            )
        )
        db.commit()
    return ticket


def test_超时的活动工单被转人工并写明原因(db_real):
    ticket = _ticket(db_real)
    reaped = sla.reap_overdue(db_real)
    assert [row.id for row in reaped] == [ticket.id]
    assert ticket.status == "escalated"
    assert ticket.escalation_reason == "sla_overdue"


def test_交接那条轨迹带着最后几步上下文(db_real):
    """文档§4 第 8 步要求"转人工并带上上下文"，而不只是改个状态。"""
    ticket = _ticket(db_real)
    sla.reap_overdue(db_real)
    # 只看那条决定：通知没发出去还会另留一条痕迹，那是另一件事
    event = (
        db_real.query(TicketEvent)
        .filter_by(ticket_id=ticket.id, node="escalate", kind="decision")
        .one()
    )
    assert "超过 SLA" in event.message
    assert "act/tool_result" in event.message and "还是 paid" in event.message


def test_没有痕迹时也要说清楚而不是给一段空话(db_real):
    ticket = _ticket(db_real, with_trace=False)
    sla.reap_overdue(db_real)
    event = (
        db_real.query(TicketEvent)
        .filter_by(ticket_id=ticket.id, node="escalate", kind="decision")
        .one()
    )
    assert "还没有任何执行痕迹" in event.message


def test_超时转人工也要把人叫来(db_real):
    """超时不会自己被人发现，只改状态的话效果就是"队列里多了几张红单"。"""
    from models import Notification, User

    now = naive_now()
    ticket = _ticket(db_real)
    ticket.assignee_id = "seat-9"
    db_real.add(
        User(
            id="seat-9", email="seat9@corp.com", username="seat9", hashed_password="x",
            role="user", created_at=now, updated_at=now,
        )
    )
    db_real.commit()
    sla.reap_overdue(db_real)
    note = db_real.query(Notification).filter_by(ticket_id=ticket.id).one()
    assert note.kind == "ticket_handoff" and "sla_overdue" in note.body


def test_等人批的工单不被超时抢走(db_real):
    """审批收件箱里它有自己的时效呈现。这里再判一次超时会把
    一张正摆在人面前的单子悄悄挪走，而那个人点进去会发现单子没了。"""
    ticket = _ticket(db_real, status="awaiting_approval")
    assert sla.reap_overdue(db_real) == []
    assert ticket.status == "awaiting_approval"


def test_已办结与已转人工的不再被动(db_real):
    for status in ("resolved", "closed", "escalated", "failed"):
        _ticket(db_real, status=status)
    assert sla.reap_overdue(db_real) == []
    assert (
        db_real.query(Ticket).filter(Ticket.status == "resolved", Ticket.escalation_reason.is_not(None)).count()
        == 0
    )


def test_没设SLA的工单不会被判超时(db_real):
    """TICKET_SLA_HOURS=0 时不写 sla_due_at，那就不该有"超时"这回事。"""
    now = naive_now()
    ticket = Ticket(
        id="t-no-sla",
        workspace_id=WS,
        channel="web_chat",
        status="new",
        request_text="随便问问",
        created_at=now - timedelta(days=90),
        updated_at=now,
        sla_due_at=None,
    )
    db_real.add(ticket)
    db_real.commit()
    assert sla.reap_overdue(db_real) == []
    assert sla.is_overdue(ticket) is False


def test_还没到点的不动(db_real):
    ticket = _ticket(db_real, hours_ago=1)
    assert sla.reap_overdue(db_real) == []
    assert ticket.status == "new"


def test_按工作区隔离(db_real):
    _ticket(db_real)
    assert sla.reap_overdue(db_real, workspace_id="other-workspace") == []
    assert len(sla.reap_overdue(db_real, workspace_id=WS)) == 1


def test_汇总只数不改状态(db_real):
    _ticket(db_real)
    summary = sla.summarize_overdue(db_real, workspace_id=WS)
    assert summary["overdue"] == 1
    assert db_real.query(Ticket).filter_by(status="new").count() == 1
