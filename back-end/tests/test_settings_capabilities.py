"""``GET /settings`` 的 capabilities 块。

前端**据此改变行为**，不只是显示一个开关状态。所以这里钉的是"报的是能力还是
开关值"——两者在文件能力上会分叉：``TOOL_FS_ENABLED`` 开着但用户一个文件夹都没
授权时，那六个工具根本不注册，而界面如果只看开关值就会摆出一个点开是空的文件树。

同样的坑 ``webSearch`` 早就踩过一次（开关开了但没配 key），注释就写在路由里。
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


def _fs(client, headers):
    return client.get("/settings", headers=headers).json()["capabilities"]["fs"]


def test_未登录拿不到设置(client):
    assert client.get("/settings").status_code in (401, 403)


def test_关着的时候报不可用(client, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", False)
    body = _fs(client, _auth_headers(client))

    assert body["enabled"] is False
    assert body["hasRoots"] is False


def test_开着但没授权时hasRoots为假(client, monkeypatch):
    """这是界面最需要区分的一种状态：功能开着、但点开文件树是空的。

    只报开关值的话界面会摆出一个空面板，而正确的动作是引导用户去设置里选一个
    文件夹。
    """
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    body = _fs(client, _auth_headers(client))

    assert body["enabled"] is True
    assert body["hasRoots"] is False


def test_授权之后hasRoots为真(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    headers = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    assert _fs(client, headers)["hasRoots"] is True


def test_hasRoots是per_user的(client, tmp_path, monkeypatch):
    """授权是按用户的。少了 user_id 过滤，别人授权过就会让我这边也显示有文件夹。"""
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    alice = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=alice)

    bob = _auth_headers(client, email="bob@example.com", username="bob")
    assert _fs(client, alice)["hasRoots"] is True
    assert _fs(client, bob)["hasRoots"] is False


def test_写与删的开关分别报出来(client, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_DELETE_ENABLED", False)
    body = _fs(client, _auth_headers(client))

    assert body["writeEnabled"] is True
    assert body["deleteEnabled"] is False


def test_不泄露本机绝对路径(client, tmp_path, monkeypatch):
    """能力清单里不该出现路径。用户列目录时自己会看到，但这里没必要带。"""
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    headers = _auth_headers(client)
    client.post("/fs/roots", json={"path": str(tmp_path)}, headers=headers)

    body = client.get("/settings", headers=headers).text
    assert str(tmp_path) not in body


# ========== 完整能力档案（agent 块） ==========


def _agent(client, headers):
    return client.get("/settings", headers=headers).json()["capabilities"]["agent"]


def test_agent能力档案反映当前配置(client, monkeypatch):
    """规划/护栏/记忆/视觉/向量库/备用模型都报的是生效值，运营方一眼看得到。"""
    monkeypatch.setattr(settings, "AGENT_PLAN_MODE", "plan_execute")
    monkeypatch.setattr(settings, "GUARDRAIL_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_WEB_FETCH_ENABLED", True)
    monkeypatch.setattr(settings, "VISION_MODELS", "glm-4v, gpt-4o")
    monkeypatch.setattr(settings, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "glm-4-flash,glm-4.5-air")

    body = _agent(client, _auth_headers(client))

    assert body["planMode"] == "plan_execute"
    assert body["guardrails"] is True
    assert body["memory"] is False
    assert body["webFetch"] is True
    assert body["vectorStore"] == "qdrant"
    # 逗号分隔的配置拆成去空列表，而不是把原串丢给前端自己拆
    assert body["visionModels"] == ["glm-4v", "gpt-4o"]
    assert body["fallbackModels"] == ["glm-4-flash", "glm-4.5-air"]


def test_agent备用链未配时为空列表(client, monkeypatch):
    """空 = 没有降级路径。报空列表而不是 null，前端不必做两种空值判断。"""
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "")
    monkeypatch.setattr(settings, "VISION_MODELS", "")

    body = _agent(client, _auth_headers(client))

    assert body["fallbackModels"] == []
    assert body["visionModels"] == []


def test_agent报的是resolve后的提示词版本(client):
    """promptVersion 是生效版本，不是配置项的字面空串。

    留空时配置项是空串，而 resolve_version 会落到契约默认版——报空串会让
    运营方以为"没提示词"，而实际有一个生效的版本。
    """
    from services import prompt_library

    body = _agent(client, _auth_headers(client))
    assert body["promptVersion"] == prompt_library.resolve_version("chat_system_rag")
    assert body["promptVersion"]  # 非空
