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


# ========== 文档的管理可见性(真正的闸口在路由) ==========
#
# 服务层的 listable_documents 不判角色:传 include_member_private=True 它就照办。
# 所以"admin 能看到成员私有文档、普通成员不能"这条规则,唯一的实现点是路由里
# 那一句 include_member_private=is_admin(current_user)。它必须在真实 HTTP 链路上测,
# 服务层测试证明不了它——那里 flag 是手传的。


def _join(client, token, code):
    # 请求体是 snake_case(``JoinRequest.invite_code``),而响应里的邀请码是
    # camelCase(``inviteCode``)。前端 client.ts 两边都对得上,这里跟着它写。
    response = client.post(
        "/workspace/join", json={"invite_code": code}, headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _private_document(db_session, *, workspace_id, owner_id, name):
    from models import Document

    document = Document(
        name=name,
        size=10,
        content="私密内容",
        workspace_id=workspace_id,
        user_id=owner_id,
        visibility="private",
        status="indexed",
        chunks=1,
    )
    db_session.add(document)
    db_session.commit()
    return document


def _team_of_two(client, db_session):
    """alice 是 admin,bob 凭邀请码加入成为 user。返回两人的 token 与 id。"""
    from models import User

    _register(client, email="alice@example.com", username="alice")
    _register(client, email="bob@example.com", username="bob")
    alice_token = _login(client, email="alice@example.com")
    bob_token = _login(client, email="bob@example.com")

    workspace = client.get("/workspace", headers=_auth(alice_token)).json()
    _join(client, bob_token, workspace["inviteCode"])

    db_session.expire_all()
    alice = db_session.query(User).filter(User.email == "alice@example.com").first()
    bob = db_session.query(User).filter(User.email == "bob@example.com").first()
    assert bob.workspace_id == workspace["id"] and bob.role == "user"
    return {
        "workspace_id": workspace["id"],
        "alice": alice,
        "bob": bob,
        "alice_token": alice_token,
        "bob_token": bob_token,
    }


def test_admin_sees_member_private_documents_over_http(client, db_session):
    """admin 的列表里有成员的个人文档,且标着"不参与我的检索"。"""
    team = _team_of_two(client, db_session)
    doc = _private_document(
        db_session,
        workspace_id=team["workspace_id"],
        owner_id=team["bob"].id,
        name="bob-私人.md",
    )

    listed = client.get("/knowledge/documents", headers=_auth(team["alice_token"]))
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()}
    assert doc.id in by_id, "admin 应当看得见成员的个人文档"
    assert by_id[doc.id]["isOwn"] is False
    assert by_id[doc.id]["retrievable"] is False, "可见但不进 admin 的检索"
    assert by_id[doc.id]["ownerName"] == "bob"


def test_member_does_not_see_another_members_private_documents_over_http(
    client, db_session
):
    """反过来那一半:普通成员看不到别人的个人文档。

    这一条挂了就是越权,而它只会在路由那一句 flag 写错时挂——服务层全绿。
    """
    team = _team_of_two(client, db_session)
    alice_private = _private_document(
        db_session,
        workspace_id=team["workspace_id"],
        owner_id=team["alice"].id,
        name="alice-私人.md",
    )

    listed = client.get("/knowledge/documents", headers=_auth(team["bob_token"]))
    assert listed.status_code == 200
    assert alice_private.id not in {item["id"] for item in listed.json()}


def test_admin_deleting_a_member_private_document_is_403_not_404(client, db_session):
    """能看见就该给出诚实的理由。

    那一篇明明在 admin 的列表里,报 404 说"不存在"会让他以为列表坏了;403 带上
    "这是他人的个人文档"才说清了为什么。而文档必须仍然在库里。
    """
    from models import Document

    team = _team_of_two(client, db_session)
    doc = _private_document(
        db_session,
        workspace_id=team["workspace_id"],
        owner_id=team["bob"].id,
        name="bob-私人.md",
    )

    response = client.delete(
        f"/knowledge/documents/{doc.id}", headers=_auth(team["alice_token"])
    )
    assert response.status_code == 403, response.text
    assert "个人文档" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.query(Document).filter(Document.id == doc.id).first() is not None


def test_adopted_orphan_becomes_a_deletable_shared_document_over_http(
    client, db_session
):
    """离职者留下的个人文档收编后，admin 能在 HTTP 层真的把它删掉。

    这是这条链路的**终点**：此前那种文档谁都删不掉（``require_can_modify`` 走私有
    那一支，NULL 不等于任何人的 id），所以只测"能列出来"不够——要测到真的能处置。
    """
    from models import Document
    from services import workspace_service

    team = _team_of_two(client, db_session)
    orphan = _private_document(
        db_session,
        workspace_id=team["workspace_id"],
        owner_id=None,  # 账号已被删除
        name="离职者-资料.md",
    )

    # 收编前：admin 的列表里没有它
    listed = client.get("/knowledge/documents", headers=_auth(team["alice_token"]))
    assert orphan.id not in {item["id"] for item in listed.json()}

    assert workspace_service.adopt_orphaned_documents(db_session) == 1

    listed = client.get("/knowledge/documents", headers=_auth(team["alice_token"]))
    by_id = {item["id"]: item for item in listed.json()}
    assert orphan.id in by_id
    assert by_id[orphan.id]["visibility"] == "workspace"
    assert by_id[orphan.id]["inherited"] is True, "界面要据此打「继承」标记"
    assert by_id[orphan.id]["retrievable"] is True, "共享文档进检索"

    deleted = client.delete(
        f"/knowledge/documents/{orphan.id}", headers=_auth(team["alice_token"])
    )
    assert deleted.status_code == 200, deleted.text
    db_session.expire_all()
    assert db_session.query(Document).filter(Document.id == orphan.id).first() is None


def test_member_deleting_an_unseen_private_document_is_404(client, db_session):
    """看不见的仍然是 404:不该因为"你不是管理员"就承认存在这么一篇文档。"""
    team = _team_of_two(client, db_session)
    alice_private = _private_document(
        db_session,
        workspace_id=team["workspace_id"],
        owner_id=team["alice"].id,
        name="alice-私人.md",
    )

    response = client.delete(
        f"/knowledge/documents/{alice_private.id}", headers=_auth(team["bob_token"])
    )
    assert response.status_code == 404, response.text


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