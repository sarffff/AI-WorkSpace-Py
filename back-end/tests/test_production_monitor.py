"""线上健康监控测试：以工单为单位的指标聚合、阈值判定、样本门槛、告警去重/节流。

用 db_real（内存 SQLite 真建表）——要断言的是跨工单聚合与 role 收口，替身测不出来。
节流是进程内全局状态，每个相关测试先把它清零。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from config import settings
from models import Notification, Ticket, User
from services import production_monitor
from services.clock import naive_now


def _ticket(
    db,
    *,
    status="resolved",
    resolution="resolved_refund",
    cost=None,
    minutes_ago=30,
    handle_minutes=None,
):
    created = naive_now() - timedelta(minutes=minutes_ago)
    resolved = (
        created + timedelta(minutes=handle_minutes) if handle_minutes is not None else None
    )
    row = Ticket(
        workspace_id="w1",
        channel="web_chat",
        status=status,
        resolution=resolution if status == "resolved" else None,
        request_text="要退款",
        llm_cost=Decimal(str(cost)) if cost is not None else None,
        created_at=created,
        updated_at=created,
        resolved_at=resolved,
    )
    db.add(row)
    db.commit()
    return row


def _admin(db, *, uid="admin1", email="admin@x.com", role="admin", active=True):
    db.add(User(id=uid, email=email, role=role, is_active=active))
    db.commit()


def _reset_throttle(monkeypatch):
    monkeypatch.setattr(production_monitor, "_last_eval_at", None)


# ---- 聚合 ----------------------------------------------------------------


def test_按工单状态算失败率与人工介入率(db_real):
    for _ in range(3):
        _ticket(db_real)
    _ticket(db_real, status="failed", resolution=None)
    _ticket(db_real, status="awaiting_approval", resolution=None)
    _ticket(db_real, status="escalated", resolution=None)

    report = production_monitor.health_report(db_real)
    assert report["totalTickets"] == 6
    assert report["metrics"]["errorRate"] == round(1 / 6, 4)
    # 挂在审批上 与 已转人工 都算"这单需要人手"，两者一起决定坐席工作量
    assert report["metrics"]["interventionRate"] == round(2 / 6, 4)


def test_窗口之外的工单不算(db_real):
    _ticket(db_real, minutes_ago=60 * 24 * 30)
    report = production_monitor.health_report(db_real, window_hours=24)
    assert report["totalTickets"] == 0
    assert report["sufficient"] is False


def test_成本未知不计入均值也不计入分位(db_real):
    _ticket(db_real, cost=0.2)
    _ticket(db_real, cost=None)
    metrics = production_monitor.health_report(db_real)["metrics"]
    assert metrics["ticketsWithKnownCost"] == 1
    assert metrics["avgCostPerTicket"] == 0.2


def test_处理时长只算已解决的(db_real):
    _ticket(db_real, handle_minutes=10)
    _ticket(db_real, status="escalated", resolution=None)
    metrics = production_monitor.health_report(db_real)["metrics"]
    assert metrics["p95HandleMs"] == 10 * 60 * 1000


# ---- 阈值判定 ------------------------------------------------------------


def test_样本不足时不判阈值(db_real, monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 5)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _ticket(db_real, status="failed", resolution=None)
    report = production_monitor.health_report(db_real)
    assert report["sufficient"] is False
    assert report["breaches"] == []  # 一张单 100% 失败率是噪声，不是信号


def test_越阈值列出对应指标(db_real, monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    monkeypatch.setattr(settings, "MONITOR_MAX_COST_PER_TICKET", 0.05)
    _ticket(db_real, status="failed", resolution=None, cost=0.9)
    breaches = production_monitor.health_report(db_real)["breaches"]
    assert {b["metric"] for b in breaches} == {"errorRate", "avgCostPerTicket"}


def test_阈值为零表示不看这一条(db_real, monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.0)
    _ticket(db_real, status="failed", resolution=None)
    assert production_monitor.health_report(db_real)["breaches"] == []


# ---- 告警 ----------------------------------------------------------------


def test_越阈值给每个在岗管理员发一条(db_real, monkeypatch):
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real, uid="a1", email="a1@x.com")
    _admin(db_real, uid="a2", email="a2@x.com")
    _ticket(db_real, status="failed", resolution=None)

    created = production_monitor.evaluate_and_alert(db_real, force=True)
    assert len(created) == 2
    kinds = {
        row.kind for row in db_real.query(Notification).filter(Notification.kind == "health_alert")
    }
    assert kinds == {"health_alert"}


def test_同一管理员的未读告警不叠加(db_real, monkeypatch):
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real)
    _ticket(db_real, status="failed", resolution=None)

    assert len(production_monitor.evaluate_and_alert(db_real, force=True)) == 1
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []


def test_没有在岗管理员时不发也不崩(db_real, monkeypatch):
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real, active=False)
    _ticket(db_real, status="failed", resolution=None)
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []


def test_节流让间隔内的第二次调用空手返回(db_real, monkeypatch):
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MIN_TICKETS", 1)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    monkeypatch.setattr(settings, "MONITOR_INTERVAL_MINUTES", 15)
    _admin(db_real)
    _ticket(db_real, status="failed", resolution=None)

    assert len(production_monitor.evaluate_and_alert(db_real)) == 1
    # 第二次落在节流窗口内：连聚合查询都不该发
    assert production_monitor.evaluate_and_alert(db_real) == []


def test_关掉监控时什么都返回空(db_real, monkeypatch):
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", False)
    _ticket(db_real, status="failed", resolution=None)
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []
