"""Router 集成层测试：真实 ASGI 链路 + SQLite 会话 + JWT 全流程。

服务层测试证明了"循环怎么走"，这一层证明"请求怎么进来"：注册/登录/带 token
访问、跨用户越权在路由层被 404、安全响应头、以及附件上传的完整 HTTP 链路。
main 在测试内延迟导入，避免 import 时挂载 /uploads 与测试目录错位。
"""
from __future__ import annotations

import pytest
import sqlalchemy

from config import settings

PASSWORD = "Passw0rd123"


@pytest.fixture()
def db_session():
    from database import Base

    import models  # noqa: F401  确保所有表已注册

    engine = sqlalchemy.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},  # TestClient 在独立线程跑 ASGI
        poolclass=sqlalchemy.pool.StaticPool,  # 内存库共享单连接，各线程看到同一张表
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
def app(db_session):
    from main import app
    from database import get_db

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    # 限流按 IP 计数，测试共享同一个 "testclient" IP，会撞 5/min 上限
    app.state.limiter.enabled = False
    try:
        yield app
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as client:  # noqa: SIM117
        yield client


def _register(client, *, email="alice@example.com", username="alice", password=PASSWORD):
    response = client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client, email="alice@example.com", password=PASSWORD) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ========== 认证全流程 ==========


def test_register_login_and_protected_access(client, db_session):
    """注册 → 登录 → 带 token 访问受保护接口 → 无 token 被 401 挡下。"""
    _register(client)

    token = _login(client)

    me = client.get("/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"

    chats = client.get("/chats", headers=_auth(token))
    assert chats.status_code == 200
    assert chats.json() == []

    anonymous = client.get("/chats")
    assert anonymous.status_code == 401


def test_register_creates_personal_workspace(client, db_session):
    """注册即建个人空间：新用户是 admin，不是无家可归。"""
    _register(client)
    token = _login(client)
    from models import User

    user = db_session.query(User).filter(User.email == "alice@example.com").first()
    assert user is not None
    assert user.workspace_id is not None
    assert user.role == "admin"
    assert client.get("/auth/me", headers=_auth(token)).status_code == 200


def test_weak_password_rejected(client):
    """密码必须含大小写字母与数字：弱口令在入口处就被 422。"""
    response = client.post(
        "/auth/register",
        json={"email": "bob@example.com", "username": "bob", "password": "password"},
    )
    assert response.status_code == 422


def test_login_wrong_password_rejected(client):
    _register(client)
    response = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "WrongPass1"}
    )
    assert response.status_code in (401, 403)


# ========== 跨用户越权 ==========


def test_cross_user_chat_access_returns_404(client, db_session):
    """别人的对话在路由层是"不存在"：读消息、工具轨迹、重命名、删除全部 404。"""
    _register(client, email="alice@example.com", username="alice")
    _register(client, email="bob@example.com", username="bob")
    alice_token = _login(client, email="alice@example.com")
    bob_token = _login(client, email="bob@example.com")

    created = client.post("/chats", json={"title": "机密"}, headers=_auth(alice_token))
    assert created.status_code == 200
    chat_id = created.json()["id"]

    for method, path in [
        ("get", f"/chats/{chat_id}/messages"),
        ("get", f"/chats/{chat_id}/tool-steps"),
        ("patch", f"/chats/{chat_id}"),
        ("delete", f"/chats/{chat_id}"),
    ]:
        kwargs = {"headers": _auth(bob_token)}
        if method == "patch":
            kwargs["json"] = {"title": "改名"}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, f"{method} {path} -> {response.status_code}"

    # 所有者本人仍可访问
    own = client.get(f"/chats/{chat_id}/messages", headers=_auth(alice_token))
    assert own.status_code == 200


# ========== 安全响应头 ==========


def test_security_headers_on_all_responses(client):
    """每个响应都带基础安全头：浏览器层面先挡住脚本注入面。"""
    for path in ("/", "/metrics/usage"):
        response = client.get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-XSS-Protection"] == "1; mode=block"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


# ========== 指标 ==========


def test_metrics_usage_requires_auth_and_returns_totals(client, db_session):
    _register(client)
    token = _login(client)

    assert client.get("/metrics/usage").status_code == 401

    response = client.get("/metrics/usage", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert "totals" in body
    assert body["totals"]["spans"] == 0
    assert "byModel" in body and "cache" in body


# ========== 附件上传（HTTP 链路） ==========


def test_attachment_upload_http_flow(monkeypatch, db_session):
    """multipart 上传 → 200 + 可访问的 URL → /uploads 静态服务能取回原文件。"""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    td = TemporaryDirectory()
    monkeypatch.setattr(settings, "UPLOAD_DIR", td.name)

    from main import app
    from database import get_db
    from fastapi.testclient import TestClient

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.state.limiter.enabled = False
    try:
        with TestClient(app) as client:
            _register(client)
            token = _login(client)
            response = client.post(
                "/chats/attachments/upload",
                files={
                    "file": (
                        "meeting.md",
                        b"# Meeting notes\n- budget 5000",
                        "text/markdown",
                    )
                },
                headers=_auth(token),
            )
            assert response.status_code == 200, response.text
            url = response.json()["url"]
            assert url.startswith("/uploads/")

            # 落盘在测试目录里，文件名只有 uuid + 扩展名
            from pathlib import Path

            stored = Path(td.name) / url.removeprefix("/uploads/")
            assert stored.exists()
            assert stored.read_bytes() == b"# Meeting notes\n- budget 5000"

            # 恶意扩展名在入口被拦：可执行/内嵌脚本类型不许传
            blocked = client.post(
                "/chats/attachments/upload",
                files={"file": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
                headers=_auth(token),
            )
            assert blocked.status_code == 400
    finally:
        app.dependency_overrides.clear()
        td.cleanup()


def test_attachment_spoofed_image_rejected(monkeypatch, db_session):
    """图片内容与扩展名不符（文本伪装成 png）在 HTTP 入口被拒。"""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    td = TemporaryDirectory()
    monkeypatch.setattr(settings, "UPLOAD_DIR", td.name)

    from main import app
    from database import get_db
    from fastapi.testclient import TestClient

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.state.limiter.enabled = False
    try:
        with TestClient(app) as client:
            _register(client)
            token = _login(client)
            response = client.post(
                "/chats/attachments/upload",
                files={"file": ("fake.png", b"not a real image", "image/png")},
                headers=_auth(token),
            )
            assert response.status_code == 400
            assert "伪造" in response.json()["detail"]
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()
        td.cleanup()