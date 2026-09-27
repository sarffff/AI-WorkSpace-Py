"""成员管理接口：真实 ASGI 链路 + JWT。

``test_workspace_members`` 证明了语义（两条不变量、私有文档不需要迁移），
这一层证明的是另外三件只有走完整链路才看得到的事：

1. **跨工作区越权在路由层被挡住。** service 的签名里带着 ``current_user``，
   看起来天然隔离，而路由层可能把它取错。
2. **未登录进不来。** 成员管理是纯管理接口，漏一个 Depends 的后果是任何人
   都能改角色。
3. **响应里带整份 workspace_info。** 前端那张面板靠它同时更新列表与
   ``adminCount``，少了就得再发一次 GET，而那会闪一下。
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


def _register(client, email, username):
    client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    token = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _team(client):
    """建一个两人工作区，返回 (admin 头, member 头, member_id)。

    走真实的邀请码链路而不是直接改库：这一层测的就是链路。
    """
    admin = _register(client, "admin@example.com", "adminuser")
    code = client.get("/workspace", headers=admin).json()["inviteCode"]

    member = _register(client, "member@example.com", "memberuser")
    client.post("/workspace/join", json={"invite_code": code}, headers=member)

    info = client.get("/workspace", headers=admin).json()
    member_id = next(
        row["id"] for row in info["members"] if row["email"] == "member@example.com"
    )
    return admin, member, member_id


# ========== 未登录 ==========


def test_未登录不能改角色也不能移除(client):
    assert client.patch(
        "/workspace/members/whoever", json={"role": "admin"}
    ).status_code in (401, 403)
    assert client.delete("/workspace/members/whoever").status_code in (401, 403)


# ========== 名册 ==========


def test_admin拿到管理字段普通成员拿不到(client):
    admin, member, _member_id = _team(client)

    as_admin = client.get("/workspace", headers=admin).json()
    assert as_admin["memberCount"] == 2
    assert as_admin["adminCount"] == 1
    assert all("email" in row for row in as_admin["members"])

    as_member = client.get("/workspace", headers=member).json()
    # 名册可见
    assert as_member["memberCount"] == 2
    # 管理字段不可见
    assert all("email" not in row for row in as_member["members"])
    # 邀请码也不给（既有行为，顺手钉住）
    assert as_member["inviteCode"] is None


# ========== 改角色 ==========


def test_提升成员并在响应里返回新的adminCount(client):
    admin, _member, member_id = _team(client)

    response = client.patch(
        f"/workspace/members/{member_id}", json={"role": "admin"}, headers=admin
    )

    assert response.status_code == 200
    body = response.json()
    assert body["member"]["role"] == "admin"
    # 整份 info 一起回来：前端不用再发一次 GET
    assert body["workspace"]["adminCount"] == 2


def test_普通成员改不了角色(client):
    admin, member, _member_id = _team(client)
    admin_id = next(
        row["id"]
        for row in client.get("/workspace", headers=admin).json()["members"]
        if row["email"] == "admin@example.com"
    )

    response = client.patch(
        f"/workspace/members/{admin_id}", json={"role": "user"}, headers=member
    )

    assert response.status_code == 400
    assert "管理员" in response.json()["detail"]


def test_最后一个管理员降级被拒(client):
    admin, _member, _member_id = _team(client)
    admin_id = next(
        row["id"]
        for row in client.get("/workspace", headers=admin).json()["members"]
        if row["isSelf"]
    )

    response = client.patch(
        f"/workspace/members/{admin_id}", json={"role": "user"}, headers=admin
    )

    assert response.status_code == 400
    assert "最后一个管理员" in response.json()["detail"]


def test_非法角色走service的中文错误而不是422(client):
    """用 Literal 会让 FastAPI 挡成 422，而 422 的 detail 是 pydantic 结构体，
    面板上只会显示"请求参数错误"。
    """
    admin, _member, member_id = _team(client)

    response = client.patch(
        f"/workspace/members/{member_id}", json={"role": "owner"}, headers=admin
    )

    assert response.status_code == 400
    assert "只能是" in response.json()["detail"]


# ========== 移除 ==========


def test_移除成员之后名册少一个而且他自己换了空间(client):
    admin, member, member_id = _team(client)

    response = client.delete(f"/workspace/members/{member_id}", headers=admin)

    assert response.status_code == 200
    body = response.json()
    assert body["removed"]["id"] == member_id
    assert body["workspace"]["memberCount"] == 1

    # 被移除的人下一次访问会拿到一个新的个人空间（懒初始化），而且他在那里是 admin
    after = client.get("/workspace", headers=member).json()
    assert after["memberCount"] == 1
    assert after["isAdmin"] is True
    assert after["id"] != body["workspace"]["id"]


def test_不能移除自己(client):
    admin, _member, _member_id = _team(client)
    admin_id = next(
        row["id"]
        for row in client.get("/workspace", headers=admin).json()["members"]
        if row["isSelf"]
    )

    response = client.delete(f"/workspace/members/{admin_id}", headers=admin)

    assert response.status_code == 400
    assert "不能移除自己" in response.json()["detail"]


def test_普通成员移除不了任何人(client):
    admin, member, _member_id = _team(client)
    admin_id = next(
        row["id"]
        for row in client.get("/workspace", headers=admin).json()["members"]
        if row["email"] == "admin@example.com"
    )

    response = client.delete(f"/workspace/members/{admin_id}", headers=member)

    assert response.status_code == 400
    assert "管理员" in response.json()["detail"]


def test_跨工作区改不了别人空间的成员(client):
    """越权的核心用例：另一个空间的 admin 拿着合法 token 去动我的成员。

    错误消息统一成"不在这个工作区"，不回 404——404 会确认这个 id 真的存在。
    """
    admin, _member, member_id = _team(client)
    outsider = _register(client, "outsider@example.com", "outsider")
    # outsider 在自己的个人空间里是 admin，所以 require_admin 不会先把他挡掉
    assert client.get("/workspace", headers=outsider).json()["isAdmin"] is True

    patched = client.patch(
        f"/workspace/members/{member_id}", json={"role": "admin"}, headers=outsider
    )
    deleted = client.delete(f"/workspace/members/{member_id}", headers=outsider)

    assert patched.status_code == 400
    assert "不在这个工作区" in patched.json()["detail"]
    assert deleted.status_code == 400
    assert "不在这个工作区" in deleted.json()["detail"]

    # 而我的成员一点没变
    assert client.get("/workspace", headers=admin).json()["memberCount"] == 2
