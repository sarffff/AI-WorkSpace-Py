"""工单通知：谁收到、收到几条、没人可发时会怎样。

用 ``db_real``：收件人选择靠的是真实的 user/workspace 关联，去重靠的是
notification 表上的查询，FakeDB 一样也测不出来。
"""
import pytest

from config import settings
from models import Notification, Ticket, User
from services.clock import naive_now
from services.ticket import alerts
from services.ticket.trace import replay as replay_events


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_AGENT_ENABLED", True)


def _workspace(db, workspace_id="w1"):
    from models import Workspace as W

    row = W(id=workspace_id, name="测试区", invite_code=f"inv-{workspace_id}", created_at=naive_now())
    db.add(row)
    db.commit()
    return row


def _user(db, *, user_id, workspace_id, role, email=None):
    user = User(
        id=user_id,
        email=email or f"{user_id}@corp.com",
        username=user_id,
        name=user_id,
        hashed_password="x",
        workspace_id=workspace_id,
        role=role,
        created_at=naive_now(),
        updated_at=naive_now(),
    )
    db.add(user)
    db.commit()
    return user


def _ticket(db, *, workspace_id="w1", assignee=None, ticket_id="t1"):
    now = naive_now()
    ticket = Ticket(
        id=ticket_id,
        workspace_id=workspace_id,
        assignee_id=assignee,
        channel="web_chat",
        status="new",
        request_text="订单 ORD-1 要退款 500 元",
        subject="ORD-1 退款",
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_有受理人时只发给受理人(db_real):
    _workspace(db_real)
    _user(db_real, user_id="seat1", workspace_id="w1", role="user")
    _user(db_real, user_id="boss", workspace_id="w1", role="admin")
    ticket = _ticket(db_real, assignee="seat1")
    sent = alerts.notify_approval_needed(db_real, ticket, {"calls": [{"name": "create_refund"}], "reason": "资金类操作必须人工确认"})
    assert sent == 1
    rows = db_real.query(Notification).filter_by(ticket_id=ticket.id).all()
    assert [row.user_id for row in rows] == ["seat1"]
    assert rows[0].kind == "approval_required"
    assert "create_refund" in rows[0].body


def test_没有受理人时发给管理员而不是全工作区(db_real):
    """广播会让每个人收到一条"有件事也许该你管"，刷屏三次之后他们就开始无视它——
    那时候真正等着的审批也被无视了。"""
    _workspace(db_real)
    _user(db_real, user_id="boss1", workspace_id="w1", role="admin")
    _user(db_real, user_id="boss2", workspace_id="w1", role="admin")
    _user(db_real, user_id="seat1", workspace_id="w1", role="user")
    ticket = _ticket(db_real)
    sent = alerts.notify_handoff(db_real, ticket, "tool_failures")
    assert sent == 2
    recipients = {
        row.user_id for row in db_real.query(Notification).filter_by(kind="ticket_handoff").all()
    }
    assert recipients == {"boss1", "boss2"}


def test_别的区的管理员收不到(db_real):
    _workspace(db_real, "w1")
    _workspace(db_real, "w2")
    _user(db_real, user_id="boss2", workspace_id="w2", role="admin")
    ticket = _ticket(db_real, workspace_id="w1")
    assert alerts.notify_handoff(db_real, ticket, "sla_overdue") == 0


def test_没人可发时轨迹里要说清楚(db_real):
    """一张卡在审批上的工单如果既没有受理人也没有管理员，它会安静地躺在那里，
    而所有指标都显示正常。"""
    _workspace(db_real)
    ticket = _ticket(db_real)
    assert alerts.notify_approval_needed(db_real, ticket, {"calls": []}) == 0
    events = replay_events(db_real, ticket.id)
    assert any("没有可通知的人" in (event.message or "") for event in events)


def test_同一张工单重复挂起不刷屏(db_real):
    _workspace(db_real)
    _user(db_real, user_id="seat1", workspace_id="w1", role="user")
    ticket = _ticket(db_real, assignee="seat1")
    alerts.notify_approval_needed(db_real, ticket, {"calls": [{"name": "create_refund"}]})
    again = alerts.notify_approval_needed(db_real, ticket, {"calls": [{"name": "create_refund"}]})
    # 第二条被去重（同一 (人, 类型, 工单) 已有未读）：模型重试挂起不应该变成第二次打扰
    assert again == 0
    assert db_real.query(Notification).filter_by(ticket_id=ticket.id).count() == 1


def test_读过之后再来才是新的一件事(db_real):
    _workspace(db_real)
    _user(db_real, user_id="seat1", workspace_id="w1", role="user")
    ticket = _ticket(db_real, assignee="seat1")
    alerts.notify_approval_needed(db_real, ticket, {"calls": [{"name": "create_refund"}]})
    row = db_real.query(Notification).filter_by(ticket_id=ticket.id).one()
    row.read_at = naive_now()
    db_real.commit()
    assert alerts.notify_approval_needed(db_real, ticket, {"calls": [{"name": "create_refund"}]}) == 1
