"""审核台账接口：真实 ASGI + JWT。

重点是**跨工作区隔离**与**待办入口**。两件事只有走完整链路才测得到：

- service 层的函数签名里带着 workspace_id，看起来天然隔离，而路由层可能忘了传，
  或者传成了别的东西。
- ``pending_only`` 是这组接口存在的一半理由（转人工没有待办入口就等于扔进一个
  没人看的队列），而它是一个 query 参数——最容易在路由层被漏掉。
"""
from __future__ import annotations

import pytest
import sqlalchemy

from config import settings

PASSWORD = "Passw0rd123"


@pytest.fixture(autouse=True)
def _ledger_on(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LEDGER_ENABLED", True)


@pytest.fixture()
def db_session():
    from database import Base

    import models  # noqa: F401

    engine = sqlalchemy.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
        future=True,
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
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()


def _headers(client, *, email="alice@example.com", username="alice"):
    client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    token = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _seed(db, workspace_id, *, verdict="needs_human", subject="单据 A"):
    from services import review_service
    from services.review_consensus import ConsensusResult
    from services.structured import InputCheck, ReviewVerdict

    return review_service.record(
        db,
        workspace_id=workspace_id,
        user_id="u1",
        subject=subject,
        result=ConsensusResult(
            verdict=ReviewVerdict(
                inputs=[InputCheck(name="凭证", found=verdict != "needs_human")],
                basis=["额度标准第 3 条"],
                verdict=verdict,
                sop_name="expense",
                sop_version=1,
            ),
            runs=1,
            agreed=True,
        ),
    )


def _workspace_of(db, username="alice"):
    """从库里读工作区 id。

    ``/auth/me`` 不返回它（``UserResponse`` 里没有这个字段），而工作区是懒初始化的
    ——注册时才补建。所以这里读 users 行，不去猜。
    """
    from models import User

    return db.query(User).filter(User.username == username).one().workspace_id


# ========== 未登录 ==========


def test_未登录看不到台账(client):
    assert client.get("/reviews").status_code in (401, 403)


def test_未登录不能处置(client):
    assert client.post("/reviews/x/resolve", json={"resolution": "approved"}).status_code in (
        401,
        403,
    )


# ========== 列表 ==========


def test_台账列出本工作区的结论(client, db_session):
    headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    _seed(db_session, workspace_id, verdict="pass", subject="通过的")
    _seed(db_session, workspace_id, subject="要人看的")

    body = client.get("/reviews", headers=headers).json()
    assert {item["subject"] for item in body["items"]} == {"通过的", "要人看的"}
    # enabled 要带回来："没开这个功能"和"还没审过任何东西"在界面上长得一样
    assert body["enabled"] is True


def test_待办筛选只给需要人判断的(client, db_session):
    headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    _seed(db_session, workspace_id, verdict="pass", subject="通过的")
    _seed(db_session, workspace_id, subject="要人看的")

    body = client.get("/reviews?pending_only=true", headers=headers).json()
    assert [item["subject"] for item in body["items"]] == ["要人看的"]


def test_看不到别的工作区的结论(client, db_session):
    """service 的签名里带着 workspace_id，看起来天然隔离——而路由层可能传错。"""
    headers = _headers(client)
    _seed(db_session, "别人的工作区", subject="别人的单据")

    body = client.get("/reviews", headers=headers).json()
    assert body["items"] == []


def test_开关关着时台账为空但说清楚(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LEDGER_ENABLED", False)
    headers = _headers(client)
    body = client.get("/reviews", headers=headers).json()
    assert body["enabled"] is False


# ========== 处置 ==========


def test_处置之后从待办里消失(client, db_session):
    headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    row = _seed(db_session, workspace_id)

    response = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "approved", "note": "凭证补齐了"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["resolution"] == "approved"

    pending = client.get("/reviews?pending_only=true", headers=headers).json()
    assert pending["items"] == []


def test_处置别的工作区的结论被拒(client, db_session):
    headers = _headers(client)
    row = _seed(db_session, "别人的工作区")
    response = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "approved"},
        headers=headers,
    )
    # 400 而不是 404/403：那个区别本身就是信息
    assert response.status_code == 400


def test_非法处置值被拒(client, db_session):
    headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    row = _seed(db_session, workspace_id)
    response = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "whatever"},
        headers=headers,
    )
    assert response.status_code == 400
    assert "approved" in response.json()["detail"]


def test_重复处置被拒(client, db_session):
    """台账要能作为依据，而可以反复改写的记录作不了依据。"""
    headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    row = _seed(db_session, workspace_id)
    first = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "approved"},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "rejected"},
        headers=headers,
    )
    assert second.status_code == 400


def test_普通成员也能处置(client, db_session):
    """定规矩是管理行为（改 SOP 要 admin），按规矩复核一张单子是日常工作。

    锁给 admin 会让待办队列堵在一个人身上，而那正好是转人工要避免的形状。
    """
    from models import User
    from services import workspace_service

    admin_headers = _headers(client)
    workspace_id = _workspace_of(db_session)
    row = _seed(db_session, workspace_id)

    member_headers = _headers(client, email="bob@example.com", username="bob")
    bob = db_session.query(User).filter(User.username == "bob").one()
    bob.workspace_id = workspace_id
    bob.role = workspace_service.ROLE_USER
    db_session.commit()

    response = client.post(
        f"/reviews/{row.id}/resolve",
        json={"resolution": "approved"},
        headers=member_headers,
    )
    assert response.status_code == 200
    assert response.json()["resolvedBy"] == bob.id
