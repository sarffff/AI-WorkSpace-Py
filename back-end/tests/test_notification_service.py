"""通知收件箱测试：create/list/unread、mark_read 自作用域、同工单去重。

用 db_real（内存 SQLite 真建表）——要断言的是持久化后的行、未读过滤与自作用域，
替身测不出来。
"""
from __future__ import annotations

from models import Notification
from services.notification_service import notification_service


def test_create_list_and_unread_count(db_real):
    a = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批：write_file"
    )
    b = notification_service.create(
        db_real, user_id="u1", kind="input_required", title="待回答：X"
    )
    assert a and b
    items = notification_service.list(db_real, "u1")
    assert [n["title"] for n in items] == ["待回答：X", "待审批：write_file"]  # 新的在前
    assert all(n["read"] is False for n in items)
    assert notification_service.unread_count(db_real, "u1") == 2


def test_unread_only_filter(db_real):
    nid = notification_service.create(db_real, user_id="u1", kind="input_required", title="A")
    notification_service.create(db_real, user_id="u1", kind="input_required", title="B")
    notification_service.mark_read(db_real, "u1", nid)
    unread = notification_service.list(db_real, "u1", unread_only=True)
    assert [n["title"] for n in unread] == ["B"]
    assert notification_service.unread_count(db_real, "u1") == 1


def test_mark_read_is_self_scoped(db_real):
    nid = notification_service.create(db_real, user_id="u1", kind="input_required", title="u1 的")
    assert notification_service.mark_read(db_real, "u2", nid) is False  # 够不着别人的
    assert notification_service.unread_count(db_real, "u1") == 1
    assert notification_service.mark_read(db_real, "u1", nid) is True
    assert notification_service.unread_count(db_real, "u1") == 0


def test_dedup_skips_unread_same_ticket(db_real):
    first = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批", ticket_id="t-1"
    )
    dup = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批(重)", ticket_id="t-1"
    )
    assert first is not None and dup is None  # 同 (user,kind,ticket) 已有未读 → 跳过
    assert db_real.query(Notification).filter(Notification.ticket_id == "t-1").count() == 1
    # 已读之后，同一张工单再次挂上来才算新事件（中断恢复会重入同一个状态）
    notification_service.mark_read(db_real, "u1", first)
    again = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批(新)", ticket_id="t-1"
    )
    assert again is not None
    assert db_real.query(Notification).filter(Notification.ticket_id == "t-1").count() == 2


def test_mark_all_read_only_touches_own(db_real):
    for i in range(3):
        notification_service.create(db_real, user_id="u1", kind="ticket_handoff", title=f"n{i}")
    notification_service.create(db_real, user_id="u2", kind="ticket_handoff", title="别人的")
    assert notification_service.mark_all_read(db_real, "u1") == 3
    assert notification_service.unread_count(db_real, "u1") == 0
    assert notification_service.unread_count(db_real, "u2") == 1
