"""线上健康监控测试：指标聚合、阈值判定、样本门槛、告警去重/节流/无管理员、admin 收口。

服务层用 db_real 直接塞 AgentRun/TraceSpan/User——要断言的是跨用户聚合与 trace 成本
join，替身测不出来。节流是进程内全局状态，每个相关测试先把它清零。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from config import settings
from models import AgentRun, Notification, TraceSpan, User
from services import production_monitor
from services.clock import naive_now


def _run(db, *, status="done", interrupts=0, parent=None, trace_id=None,
         finished_after_ms=None, user_id="u1", minutes_ago=1):
    now = naive_now()
    start = now - timedelta(minutes=minutes_ago)
    finished = start + timedelta(milliseconds=finished_after_ms) if finished_after_ms else None
    row = AgentRun(
        chat_id="c1", user_id=user_id, status=status, interrupts=interrupts,
        parent_run_id=parent, trace_id=trace_id,
        started_at=start, updated_at=start, finished_at=finished,
    )
    db.add(row)
    return row


def _span(db, trace_id, *, cost=None):
    db.add(TraceSpan(
        id=uuid.uuid4().hex[:32], trace_id=trace_id, name="chat.turn", kind="llm",
        started_at=naive_now(),
        cost=Decimal(str(cost)) if cost is not None else None,
    ))


def _admin(db, *, uid="admin1", email="admin@x.com", role="admin", active=True):
    db.add(User(id=uid, email=email, role=role, is_active=active))


def _reset_throttle(monkeypatch):
    monkeypatch.setattr(production_monitor, "_last_eval_at", None)


# ========== health_report：聚合 ==========


def test_health_report_computes_rates(db_real):
    for _ in range(6):
        _run(db_real, status="done")
    _run(db_real, status="failed")
    _run(db_real, status="failed")
    _run(db_real, status="abandoned")          # 无人裁决 = 介入
    _run(db_real, status="done", interrupts=1)  # 被审批打断 = 介入
    _run(db_real, status="failed", parent="p1")  # 子代理失败：不该计入
    db_real.commit()

    report = production_monitor.health_report(db_real, min_runs=5)
    assert report["totalRuns"] == 10
    assert report["sufficient"] is True
    assert report["metrics"]["errorRate"] == 0.2
    assert report["metrics"]["interventionRate"] == 0.2


def test_health_report_insufficient_sample_no_breach(db_real, monkeypatch):
    """样本不足：即便错误率 100% 也不判超阈值——小样本的比例是噪声。"""
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    for _ in range(3):
        _run(db_real, status="failed")
    db_real.commit()
    report = production_monitor.health_report(db_real)  # 默认 min_runs=20
    assert report["totalRuns"] == 3
    assert report["sufficient"] is False
    assert report["metrics"]["errorRate"] == 1.0
    assert report["breaches"] == []  # 不足样本不告警


def test_health_report_cost_from_trace_spans(db_real):
    """单次成本按 run 的 trace_id 聚合 trace_spans.cost；NULL 成本不计（"未知"非 0）。"""
    _run(db_real, trace_id="t1")
    _span(db_real, "t1", cost="0.02")
    _span(db_real, "t1", cost="0.01")  # 同一 run 两段 → 0.03
    _run(db_real, trace_id="t2")
    _span(db_real, "t2", cost="0.05")
    _run(db_real, trace_id="t3")
    _span(db_real, "t3", cost=None)    # 未定价 → 不进成本样本
    db_real.commit()

    report = production_monitor.health_report(db_real, min_runs=1)
    m = report["metrics"]
    assert m["runsWithKnownCost"] == 2            # t3 不算
    assert m["avgCostPerRun"] == round((0.03 + 0.05) / 2, 6)


def test_health_report_latency_p95(db_real):
    """端到端延迟 p95 按 finished-started 算；没有 finished_at 的 run 不计。"""
    for ms in (100, 200, 300, 400, 1000):
        _run(db_real, finished_after_ms=ms)
    _run(db_real, finished_after_ms=None)  # 没结束：不计入延迟
    db_real.commit()
    report = production_monitor.health_report(db_real, min_runs=1)
    # 5 个样本的 p95 落在 400 与 1000 之间（线性插值），> 400
    assert report["metrics"]["p95LatencyMs"] > 400


# ========== evaluate_and_alert：告警 ==========


def test_evaluate_and_alert_disabled_noop(db_real, monkeypatch):
    """MONITOR_ENABLED 关（默认）：空转，不建任何通知。"""
    _admin(db_real)
    for _ in range(25):
        _run(db_real, status="failed")
    db_real.commit()
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []
    assert db_real.query(Notification).count() == 0


def test_evaluate_and_alert_fires_on_breach(db_real, monkeypatch):
    """开 + 错误率超阈值 + 有管理员：建一条 health_alert，正文点名错误率。"""
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real)
    for _ in range(20):
        _run(db_real, status="failed")
    db_real.commit()

    created = production_monitor.evaluate_and_alert(db_real, force=True)
    assert len(created) == 1
    note = db_real.query(Notification).one()
    assert note.kind == "health_alert"
    assert note.user_id == "admin1"
    assert "错误率" in (note.body or "")


def test_evaluate_and_alert_dedups_unread(db_real, monkeypatch):
    """同一管理员已有未读健康告警就不叠加（已读后再超阈值才算新事件）。"""
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real)
    for _ in range(20):
        _run(db_real, status="failed")
    db_real.commit()

    assert len(production_monitor.evaluate_and_alert(db_real, force=True)) == 1
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []  # 去重
    assert db_real.query(Notification).count() == 1


def test_evaluate_and_alert_throttled(db_real, monkeypatch):
    """节流：间隔内的第二次调用根本不评估。

    与去重隔离——第一条告警建好后标为已读，去重此时已允许再建；若节流失效，
    第二次非强制调用会评估并建新告警。断言它没有，才证明是节流拦下的。
    """
    _reset_throttle(monkeypatch)
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    monkeypatch.setattr(settings, "MONITOR_INTERVAL_MINUTES", 999.0)
    _admin(db_real)
    for _ in range(20):
        _run(db_real, status="failed")
    db_real.commit()

    assert len(production_monitor.evaluate_and_alert(db_real)) == 1  # 首次：评估并告警
    production_monitor.notification_service.mark_all_read(db_real, "admin1")  # 去重已放行
    assert production_monitor.evaluate_and_alert(db_real) == []  # 仍被节流拦下
    assert db_real.query(Notification).count() == 1


def test_evaluate_and_alert_no_admin_no_fire(db_real, monkeypatch):
    """超阈值但没有在岗管理员：不建通知（告警无处可发，记日志，不抛）。"""
    monkeypatch.setattr(settings, "MONITOR_ENABLED", True)
    monkeypatch.setattr(settings, "MONITOR_MAX_ERROR_RATE", 0.1)
    _admin(db_real, uid="u2", email="user@x.com", role="user")  # 只有普通用户
    for _ in range(20):
        _run(db_real, status="failed")
    db_real.commit()
    assert production_monitor.evaluate_and_alert(db_real, force=True) == []
    assert db_real.query(Notification).count() == 0


# ========== GET /metrics/health：管理员收口 ==========


import sqlalchemy  # noqa: E402
import pytest  # noqa: E402

PASSWORD = "Passw0rd123"


@pytest.fixture()
def db_session():
    from database import Base
    import models  # noqa: F401

    engine = sqlalchemy.create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    session = sqlalchemy.orm.sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient
    from main import app
    from database import get_db

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.state.limiter.enabled = False
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()


def _headers(client):
    client.post("/auth/register", json={"email": "mon@x.com", "username": "monitor_admin", "password": PASSWORD})
    token = client.post("/auth/login", json={"email": "mon@x.com", "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_health_endpoint_admin_ok(client):
    """注册用户默认 role=admin → 能看健康快照。"""
    resp = client.get("/metrics/health", headers=_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert "report" in body and "enabled" in body


def test_health_endpoint_forbids_non_admin(client, db_session):
    """普通用户 403——线上健康是跨用户的运营视图，不是个人用量。"""
    headers = _headers(client)
    db_session.query(User).update({User.role: "user"})
    db_session.commit()
    assert client.get("/metrics/health", headers=headers).status_code == 403
