"""通知收件箱测试：create/list/unread、mark_read 自作用域、去重、abandoned 钩子。

用 db_real（内存 SQLite 真建表）——要断言的是持久化后的行、未读过滤与自作用域，
替身测不出来。
"""
from __future__ import annotations

from datetime import timedelta

from config import settings
from models import AgentRun, Notification
from services import checkpoint_store
from services.clock import naive_now
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


def test_dedup_skips_unread_same_run(db_real):
    first = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批", run_id="run-1"
    )
    dup = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批(重)", run_id="run-1"
    )
    assert first is not None and dup is None  # 同 (user,kind,run) 已有未读 → 跳过
    assert db_real.query(Notification).filter(Notification.run_id == "run-1").count() == 1
    # 已读之后，同一个 run 再来才算新事件
    notification_service.mark_read(db_real, "u1", first)
    again = notification_service.create(
        db_real, user_id="u1", kind="approval_required", title="待审批(新)", run_id="run-1"
    )
    assert again is not None
    assert db_real.query(Notification).filter(Notification.run_id == "run-1").count() == 2


def test_mark_all_read_only_touches_own(db_real):
    for i in range(3):
        notification_service.create(db_real, user_id="u1", kind="input_required", title=f"n{i}")
    notification_service.create(db_real, user_id="u2", kind="input_required", title="别人的")
    assert notification_service.mark_all_read(db_real, "u1") == 3
    assert notification_service.unread_count(db_real, "u1") == 0
    assert notification_service.unread_count(db_real, "u2") == 1


def test_abandoned_run_creates_notification(db_real, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TIMEOUT_HOURS", 24)
    old = naive_now() - timedelta(hours=48)
    db_real.add(
        AgentRun(
            id="run-x", chat_id="c1", user_id="u1", status="waiting_approval",
            rounds=1, started_at=old, updated_at=old,
        )
    )
    db_real.commit()
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 1
    notes = notification_service.list(db_real, "u1")
    assert len(notes) == 1
    assert notes[0]["kind"] == "run_abandoned"
    assert notes[0]["runId"] == "run-x"
