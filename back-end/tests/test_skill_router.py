"""作业指导管理接口：真实 ASGI + JWT。

重点是**权限**：一条 SOP 影响的是全工作区所有人的执行方式，那和"上传一份自己的
资料"不是一个权限级别。普通成员能看（他们需要知道 agent 会按什么规程办事），
不能改。这件事只有走完整链路才测得到——service 层的函数签名里带着 workspace_id，
看起来天然隔离，而路由层可能忘了查角色。
"""
from __future__ import annotations

import pytest
import sqlalchemy

PASSWORD = "Passw0rd123"


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
    """注册并登录。第一个注册的人会拿到自己的工作区并成为 admin
    （见 workspace_service.resolve_for_user）。
    """
    client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    token = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides):
    body = {
        "name": "expense-flow",
        "description": "本公司的报销审核流程",
        "instructions": "只看三项：额度、凭证、时限。",
    }
    body.update(overrides)
    return body


# ========== 未登录 ==========


def test_未登录看不到(client):
    assert client.get("/skills").status_code in (401, 403)


def test_未登录不能写(client):
    assert client.put("/skills", json=_payload()).status_code in (401, 403)


# ========== 列表 ==========


def test_列表分两层且带内置(client):
    body = client.get("/skills", headers=_headers(client)).json()

    assert body["workspace"] == []
    # 仓库里那份 expense-review 应当出现在内置层
    assert any(item["name"] == "expense-review" for item in body["builtin"])
    assert body["canEdit"] is True


# ========== 增改删 ==========


def test_新增并读回(client):
    headers = _headers(client)
    response = client.put("/skills", json=_payload(), headers=headers)

    assert response.status_code == 200, response.text
    body = client.get("/skills", headers=headers).json()
    assert len(body["workspace"]) == 1
    assert body["workspace"][0]["name"] == "expense-flow"


def test_同名再PUT是更新而不是报错(client):
    """admin 最常做的操作是改一条 SOP 的正文。"""
    headers = _headers(client)
    client.put("/skills", json=_payload(), headers=headers)
    client.put(
        "/skills", json=_payload(instructions="改成看四项。"), headers=headers
    )

    body = client.get("/skills", headers=headers).json()
    assert len(body["workspace"]) == 1
    assert body["workspace"][0]["instructions"] == "改成看四项。"


def test_带空格的name被拒(client):
    """模型要用这个名字调 load_skill，带空格时它抄进参数很容易多一个或少一个。"""
    response = client.put(
        "/skills", json=_payload(name="报销 流程"), headers=_headers(client)
    )
    assert response.status_code == 400


def test_空description被拒(client):
    """它是模型选用这份指导的唯一依据。"""
    response = client.put(
        "/skills", json=_payload(description=""), headers=_headers(client)
    )
    # pydantic 的 min_length 先拦下来
    assert response.status_code == 422


def test_超长instructions被拒(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "SKILL_MAX_CHARS", 10)
    response = client.put(
        "/skills", json=_payload(instructions="x" * 50), headers=_headers(client)
    )
    assert response.status_code == 400
    assert "超过 10 字符" in response.json()["detail"]


def test_删除(client):
    headers = _headers(client)
    skill_id = client.put("/skills", json=_payload(), headers=headers).json()["id"]

    assert client.delete(f"/skills/{skill_id}", headers=headers).status_code == 200
    assert client.get("/skills", headers=headers).json()["workspace"] == []


def test_删除不存在的返回404(client):
    response = client.delete("/skills/no-such-id", headers=_headers(client))
    assert response.status_code == 404


# ========== 权限 ==========


def test_普通成员能看但不能改(client, db_session):
    """一条 SOP 影响全工作区所有人的执行方式。"""
    from services import workspace_service

    admin = _headers(client)
    client.put("/skills", json=_payload(), headers=admin)

    # 第二个人加入同一个工作区，角色是 member
    member = _headers(client, email="bob@example.com", username="bob")
    from models import User

    bob = db_session.query(User).filter(User.email == "bob@example.com").first()
    alice = db_session.query(User).filter(User.email == "alice@example.com").first()
    bob.workspace_id = alice.workspace_id
    # 字段是 ``role``，不是 ``workspace_role``——后者不存在，赋值不会报错（Python
    # 允许给实例加新属性），只是 is_admin 读的还是原来那个 role，于是 bob 仍然是
    # 管理员，而测试断言"不能改"必然失败。
    #
    # ROLE_USER 而不是 ROLE_MEMBER：后者不存在，存量库里的 "member" 是历史值
    # （ROLE_MEMBER_LEGACY），语义等同 ROLE_USER，角色判断一律走 is_admin()。
    bob.role = workspace_service.ROLE_USER
    db_session.commit()

    listed = client.get("/skills", headers=member).json()
    assert len(listed["workspace"]) == 1
    assert listed["canEdit"] is False

    assert client.put("/skills", json=_payload(), headers=member).status_code == 403


def test_同名内置被盖时列表里标出来(client):
    """不标的话 admin 会以为自己写的那份没生效。"""
    headers = _headers(client)
    client.put(
        "/skills", json=_payload(name="expense-review"), headers=headers
    )
    body = client.get("/skills", headers=headers).json()
    builtin = {item["name"]: item for item in body["builtin"]}
    assert builtin["expense-review"]["overridden"] is True
