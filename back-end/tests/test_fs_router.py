"""本机文件夹授权接口：真实 ASGI 链路 + JWT。

``fs_roots`` 的单元测试证明了沙箱与登记逻辑，这一层证明请求怎么进来，
以及**跨用户越权在路由层被挡住**——那件事只有走完整链路才测得到：
service 层的函数签名里带着 user_id，看起来天然隔离，而路由层可能把它取错。
"""
from __future__ import annotations

import pytest
import sqlalchemy

from config import settings

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


def _auth_headers(client, *, email="alice@example.com", username="alice"):
    client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    token = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ========== 未登录 ==========


def test_未登录拿不到授权列表(client):
    assert client.get("/fs/roots").status_code in (401, 403)


def test_未登录不能授权(client):
    assert client.post("/fs/roots", json={"path": "C:/"}).status_code in (401, 403)


# ========== 列表与开关状态 ==========


def test_空列表也返回开关状态(client, monkeypatch):
    """前端要能区分两种"没有文件能力"：没授权（引导去选一个）和后端没开
    （选了也没用，该说清楚而不是让用户点完发现没变化）。
    """
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", False)
    body = client.get("/fs/roots", headers=_auth_headers(client)).json()

    assert body["roots"] == []
    assert body["enabled"] is False
    assert body["tools"] == []


def test_开关打开时返回工具清单(client, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_DELETE_ENABLED", False)
    body = client.get("/fs/roots", headers=_auth_headers(client)).json()

    assert body["enabled"] is True
    assert body["writeEnabled"] is True
    assert body["deleteEnabled"] is False
    assert "read_file" in body["tools"]
    assert "delete_file" not in body["tools"]


# ========== 授权 ==========


def test_授权一个真实目录(client, tmp_path):
    headers = _auth_headers(client)
    response = client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["path"] == str(tmp_path)
    assert len(client.get("/fs/roots", headers=headers).json()["roots"]) == 1


def test_不存在的目录返回400且消息可读(client, tmp_path):
    """错误消息要能直接显示给用户。一句 400 帮不上忙——用户需要知道是路径打错了。"""
    response = client.post(
        "/fs/roots",
        json={"path": str(tmp_path / "nope")},
        headers=_auth_headers(client),
    )

    assert response.status_code == 400
    assert "不是一个存在的目录" in response.json()["detail"]


def test_文件路径不能当根授权(client, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    response = client.post(
        "/fs/roots", json={"path": str(target)}, headers=_auth_headers(client)
    )

    assert response.status_code == 400


def test_超长路径被schema挡掉(client):
    """512 与 workspace_roots.path 那一列对齐。入库时静默截断的后果是沙箱根变成
    一个**不同的目录**——前缀校验照样通过，只是通过的是错的那个。
    """
    response = client.post(
        "/fs/roots", json={"path": "C:/" + "a" * 600}, headers=_auth_headers(client)
    )
    assert response.status_code == 422


def test_重复授权同一目录不会攒出两行(client, tmp_path):
    headers = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    assert len(client.get("/fs/roots", headers=headers).json()["roots"]) == 1


# ========== 撤销与越权 ==========


def test_撤销授权(client, tmp_path):
    headers = _auth_headers(client)
    root_id = client.post(
        "/fs/roots", json={"path": str(tmp_path)}, headers=headers
    ).json()["id"]

    assert client.delete(f"/fs/roots/{root_id}", headers=headers).status_code == 200
    assert client.get("/fs/roots", headers=headers).json()["roots"] == []


def test_撤销不存在的授权返回404(client):
    response = client.delete("/fs/roots/no-such-id", headers=_auth_headers(client))
    assert response.status_code == 404


def test_别人看不到我的授权(client, tmp_path):
    """授权是 per-user 的。这一条走完整链路，因为 service 层的签名里带着 user_id、
    看起来天然隔离——真正会出错的地方是路由层把它取错。
    """
    alice = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=alice)

    bob = _auth_headers(client, email="bob@example.com", username="bob")
    assert client.get("/fs/roots", headers=bob).json()["roots"] == []


def test_别人撤不掉我的授权(client, tmp_path):
    """少了 user_id 过滤，任何人拿一个 id 就能撤销别人的授权。"""
    alice = _auth_headers(client)
    root_id = client.post(
        "/fs/roots", json={"path": str(tmp_path)}, headers=alice
    ).json()["id"]

    bob = _auth_headers(client, email="bob@example.com", username="bob")
    assert client.delete(f"/fs/roots/{root_id}", headers=bob).status_code == 404
    # alice 那边还在
    assert len(client.get("/fs/roots", headers=alice).json()["roots"]) == 1


# ========== 浏览 ==========


def test_未登录不能浏览(client):
    assert client.get("/fs/browse").status_code in (401, 403)


def test_省略path时返回授权根本身(client, tmp_path):
    """界面第一次打开时还不知道有什么，所以这里返回根列表而不是 400。"""
    headers = _auth_headers(client)
    client.post(
        "/fs/roots", json={"path": str(tmp_path), "label": "我的资料"}, headers=headers
    )

    body = client.get("/fs/browse", headers=headers).json()

    assert body["path"] is None
    assert body["parent"] is None
    assert [e["name"] for e in body["entries"]] == ["我的资料"]
    assert body["entries"][0]["isRoot"] is True
    assert body["entries"][0]["isDir"] is True


def test_没授权时省略path返回空列表(client):
    body = client.get("/fs/browse", headers=_auth_headers(client)).json()
    assert body["entries"] == []


def test_列出目录内容_目录在前文件在后(client, tmp_path):
    headers = _auth_headers(client)
    (tmp_path / "zzz_dir").mkdir()
    (tmp_path / "aaa.txt").write_text("hi", encoding="utf-8")
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    body = client.get(
        "/fs/browse", params={"path": str(tmp_path)}, headers=headers
    ).json()

    # 目录排在文件前面，即使名字排序上在后
    assert [e["name"] for e in body["entries"]] == ["zzz_dir", "aaa.txt"]
    assert body["entries"][0]["isDir"] is True
    assert body["entries"][0]["size"] is None
    assert body["entries"][1]["isDir"] is False
    assert body["entries"][1]["size"] == 2


def test_浏览跳过与模型一致的噪音目录(client, tmp_path):
    """界面和模型必须看到同一个目录。少了这一条，界面里冒出 node_modules，
    而模型的 list_directory 里没有——同一个目录两种视图，排查纯浪费。
    """
    headers = _auth_headers(client)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "real.md").write_text("x", encoding="utf-8")
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    body = client.get(
        "/fs/browse", params={"path": str(tmp_path)}, headers=headers
    ).json()

    assert [e["name"] for e in body["entries"]] == ["real.md"]


def test_越界路径被挡掉(client, tmp_path):
    """沙箱走的是和六个工具同一个 resolve_within_roots。"""
    headers = _auth_headers(client)
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    client.post("/fs/roots", json={"path": str(root)}, headers=headers)

    response = client.get(
        "/fs/browse", params={"path": str(outside)}, headers=headers
    )
    assert response.status_code == 400


def test_同前缀兄弟目录不算在内(client, tmp_path):
    """``work-secrets`` 不能因为以 ``work`` 开头就被放进来。"""
    headers = _auth_headers(client)
    root = tmp_path / "work"
    root.mkdir()
    sibling = tmp_path / "work-secrets"
    sibling.mkdir()
    client.post("/fs/roots", json={"path": str(root)}, headers=headers)

    assert (
        client.get(
            "/fs/browse", params={"path": str(sibling)}, headers=headers
        ).status_code
        == 400
    )


def test_dotdot被挡掉(client, tmp_path):
    headers = _auth_headers(client)
    root = tmp_path / "work"
    root.mkdir()
    client.post("/fs/roots", json={"path": str(root)}, headers=headers)

    assert (
        client.get(
            "/fs/browse", params={"path": str(root / ".." / "elsewhere")}, headers=headers
        ).status_code
        == 400
    )


def test_别人的授权目录我浏览不了(client, tmp_path):
    """授权是 per-user 的，浏览必须跟着同一个判据。"""
    alice = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=alice)

    bob = _auth_headers(client, email="bob@example.com", username="bob")
    assert (
        client.get("/fs/browse", params={"path": str(tmp_path)}, headers=bob).status_code
        == 400
    )


def test_文件路径不能当目录浏览(client, tmp_path):
    headers = _auth_headers(client)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    response = client.get(
        "/fs/browse", params={"path": str(target)}, headers=headers
    )
    assert response.status_code == 400
    assert "不是一个目录" in response.json()["detail"]


def test_子目录给出可用的parent(client, tmp_path):
    headers = _auth_headers(client)
    sub = tmp_path / "sub"
    sub.mkdir()
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    body = client.get("/fs/browse", params={"path": str(sub)}, headers=headers).json()

    # parent 要能真的再请求一次
    assert body["parent"] is not None
    assert (
        client.get(
            "/fs/browse", params={"path": body["parent"]}, headers=headers
        ).status_code
        == 200
    )


def test_授权根本身的parent为空(client, tmp_path):
    """根的上一级在沙箱外。给出来会让界面做一个必然 400 的请求。"""
    headers = _auth_headers(client)
    root = tmp_path / "work"
    root.mkdir()
    client.post("/fs/roots", json={"path": str(root)}, headers=headers)

    body = client.get("/fs/browse", params={"path": str(root)}, headers=headers).json()
    assert body["parent"] is None


def test_条目过多时截断并标记(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FS_LIST_MAX_ENTRIES", 3)
    headers = _auth_headers(client)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    body = client.get(
        "/fs/browse", params={"path": str(tmp_path)}, headers=headers
    ).json()

    assert len(body["entries"]) == 3
    assert body["truncated"] is True


def test_label显示成相对授权根的形式(client, tmp_path):
    headers = _auth_headers(client)
    sub = tmp_path / "sub"
    sub.mkdir()
    client.post(
        "/fs/roots", json={"path": str(tmp_path), "label": "资料"}, headers=headers
    )

    body = client.get("/fs/browse", params={"path": str(sub)}, headers=headers).json()
    assert body["label"] == "资料/sub"
