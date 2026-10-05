"""回复出口：入队去重、租约重试、显式抑制，以及"没通道时什么都不改"。

最后一条是这个文件里最要紧的断言。没有出口通道时把 pending 标成 sent，是让这张表
从此再也回答不了"客户收到回话了吗"——而它是这张表存在的唯一理由。
"""
from datetime import timedelta

import pytest

from config import settings
from models import Ticket, TicketOutbox
from services.clock import naive_now
from services.ticket import outbox


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_OUTBOX_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "TICKET_OUTBOX_LEASE_SECONDS", 120)
    monkeypatch.setattr(settings, "TICKET_OUTBOX_BACKOFF_SECONDS", 60)


def _ticket(db, *, ticket_id="t1", channel="email", workspace_id="w1") -> Ticket:
    now = naive_now()
    ticket = Ticket(
        id=ticket_id, workspace_id=workspace_id, channel=channel, status="resolved",
        request_text="ORD-1 帮我查查", created_at=now, updated_at=now,
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_办结的回话与邀评各排一条(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="您的包裹明天到", recipient="z@corp.com")
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_CSAT_INVITE, body=outbox.CSAT_INVITE_BODY, recipient="z@corp.com")
    rows = db_real.query(TicketOutbox).all()
    assert {row.kind for row in rows} == {"reply", "csat_invite"}
    assert all(row.status == "pending" for row in rows)
    assert outbox.count_pending(db_real, "w1") == 2


def test_同一张单同一种消息不重复排队(db_real):
    """重跑一次确认节点不该让客户收到两封一样的邮件。"""
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="第一遍")
    again = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="第二遍")
    assert again is None
    assert db_real.query(TicketOutbox).filter_by(kind="reply").count() == 1


def test_空正文不排队(db_real):
    ticket = _ticket(db_real)
    assert outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="   ") is None


def test_未知类型直接报错而不是静默入库(db_real):
    ticket = _ticket(db_real)
    with pytest.raises(ValueError):
        outbox.enqueue(db_real, ticket, kind="sms_spam", body="x")


def test_没有出口通道时deliver一行状态都不改(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="您的包裹明天到")
    summary = outbox.deliver(db_real, sender=None)
    assert summary["delivered"] is False and summary["pending"] == 1
    assert db_real.query(TicketOutbox).filter_by(status="pending").count() == 1
    assert db_real.query(TicketOutbox).filter_by(status="sent").count() == 0


def test_接上通道后真的发出去并留下时间戳(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到", recipient="z@corp.com")
    sent: list[tuple] = []
    summary = outbox.deliver(db_real, sender=lambda channel, recipient, body: sent.append((channel, recipient, body)))
    assert summary["sent"] == 1 and summary["pending"] == 0
    assert sent == [("email", "z@corp.com", "明天到")]
    row = db_real.query(TicketOutbox).filter_by(id=row_id).one()
    assert row.status == "sent" and row.sent_at is not None and row.attempts == 1


def test_发送失败按尝试次数退避重排(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")

    def boom(channel, recipient, body):
        raise RuntimeError("smtp 550")

    summary = outbox.deliver(db_real, sender=boom)
    row = db_real.query(TicketOutbox).one()
    assert row.status == "pending" and row.attempts == 1
    assert "smtp 550" in row.error
    # available_at 被推到未来：一条一直发不出去的消息不该每次 drain 都再打服务商一次
    assert row.available_at > naive_now() + timedelta(seconds=30)
    assert summary["sent"] == 0


def test_试满上限才落成failed(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")

    def boom(channel, recipient, body):
        raise RuntimeError("smtp 550")

    for _ in range(3):
        # 退避到未来的行要能立刻再被认领，才能测到上限这条路径
        row = db_real.query(TicketOutbox).one()
        row.available_at = naive_now()
        db_real.commit()
        outbox.deliver(db_real, sender=boom)
    assert db_real.query(TicketOutbox).one().status == "failed"
    assert db_real.query(TicketOutbox).one().attempts == 3


def test_发送中途进程没掉时租约过期会被捡回(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    claimed = outbox.claim_next(db_real, "dead-worker")
    assert claimed is not None
    row = db_real.query(TicketOutbox).one()
    row.lease_expires_at = naive_now() - timedelta(seconds=1)
    db_real.commit()
    assert outbox.reap_expired_leases(db_real) == 1
    assert db_real.query(TicketOutbox).one().status == "pending"


def test_抑制必须给理由而且要显式(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    with pytest.raises(ValueError):
        outbox.suppress(db_real, row_id, actor_id="u1", reason="  ")
    assert outbox.suppress(db_real, row_id, actor_id="u1", reason="客户已电话解决，别再发邮件")
    row = db_real.query(TicketOutbox).one()
    assert row.status == "suppressed" and "客户已电话解决" in row.error


def test_被抑制的不会再被认领出去(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    outbox.suppress(db_real, row_id, actor_id="u1", reason="已电话沟通")
    assert outbox.claim_next(db_real, "w") is None
