"""Phase 2 后端端点：回放只读快照 / 引用跳原文 / 对话导出。

- checkpoint_store.state_view 的裁剪逻辑用 db_real 单测；
- 三个 HTTP 端点用 client/db_session 跑 ownership 与作用域（引用端点的私有隔离是安全边界）。
"""
from __future__ import annotations

import sqlalchemy
import pytest

from services.clock import naive_now

PASSWORD = "Passw0rd123"


# ========== state_view 裁剪（db_real 单测）==========


def test_state_view_sanitizes_and_truncates(db_real):
    from models import AgentCheckpoint, AgentRun
    from services import checkpoint_store
    from services.agent_state import TurnState

    now = naive_now()
    state = TurnState(
        run_id="r1", chat_id="c1", user_id="u1", workspace_id="w1",
        round_index=2, phase="pre_tools", status="running",
        messages=[
            {"role": "user", "content": "问题"},
            # 多模态内容块：只应取 text，丢掉 image_url 的 base64
            {"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "X" * 5000},
        ],
        pending_calls=[{"id": "t1", "name": "search_knowledge_base", "arguments": "{\"q\":1}"}],
        pending_index=0, budget_remaining=5000, breaker_tripped=["calc"],
    )
    db_real.add(AgentRun(id="r1", chat_id="c1", user_id="u1", status="done",
                         rounds=2, started_at=now, updated_at=now))
    db_real.add(AgentCheckpoint(id="cp1", run_id="r1", seq=3, phase="pre_tools",
                                round_index=2, state=state.to_json(), created_at=now))
    db_real.commit()

    view = checkpoint_store.state_view(db_real, "r1", 3)
    assert view["round"] == 2 and view["phase"] == "pre_tools" and view["seq"] == 3
    assert view["messageCount"] == 3
    # 长消息被截到 2000
    assert len(view["messages"][-1]["content"]) == 2000
    # 多模态只留了文字、没有 base64
    assert view["messages"][1]["content"] == "看这张图"
    assert "base64" not in view["messages"][1]["content"]
    assert view["pendingCalls"] == [{"name": "search_knowledge_base", "arguments": "{\"q\":1}"}]
    assert view["budgetRemaining"] == 5000 and view["breakerTripped"] == ["calc"]


def test_state_view_missing_seq_is_none(db_real):
    from services import checkpoint_store

    assert checkpoint_store.state_view(db_real, "nope", 1) is None


# ========== HTTP 端点（client/db_session）==========


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


def _headers(client, email="p2@x.com", username="p2user"):
    client.post("/auth/register", json={"email": email, "username": username, "password": PASSWORD})
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _uid(db_session, email="p2@x.com"):
    from models import User

    return db_session.query(User).filter_by(email=email).one().id


def _seed_checkpoint(db, *, run_id, uid, seq=3):
    from models import AgentCheckpoint, AgentRun
    from services.agent_state import TurnState

    now = naive_now()
    state = TurnState(run_id=run_id, chat_id="c1", user_id=uid, workspace_id="w1",
                      round_index=1, phase="pre_tools", messages=[{"role": "user", "content": "q"}])
    db.add(AgentRun(id=run_id, chat_id="c1", user_id=uid, status="done",
                    rounds=1, started_at=now, updated_at=now))
    db.add(AgentCheckpoint(id=f"cp-{run_id}", run_id=run_id, seq=seq, phase="pre_tools",
                           round_index=1, state=state.to_json(), created_at=now))
    db.commit()


def test_checkpoint_endpoint_owner_ok(client, db_session):
    headers = _headers(client)
    _seed_checkpoint(db_session, run_id="r1", uid=_uid(db_session), seq=3)
    resp = client.get("/chats/runs/r1/checkpoints/3", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["round"] == 1 and resp.json()["seq"] == 3


def test_checkpoint_endpoint_non_owner_404(client, db_session):
    headers = _headers(client)
    _seed_checkpoint(db_session, run_id="rX", uid="someone-else", seq=1)
    assert client.get("/chats/runs/rX/checkpoints/1", headers=headers).status_code == 404


# ---- 引用跳原文 ----


def _workspace_id(client, db_session, headers):
    from models import User

    client.get("/knowledge/documents", headers=headers)  # 触发 resolve_for_user 建工作区
    db_session.expire_all()
    return db_session.query(User).filter_by(email="p2@x.com").one().workspace_id


def _seed_doc(db, *, doc_id, ws, visibility="workspace", user_id=None, chunks=("块A", "块B", "块C")):
    from models import Document, DocumentChunk

    db.add(Document(id=doc_id, name=f"{doc_id}.md", size=1, content="x", workspace_id=ws,
                    user_id=user_id, visibility=visibility, status="indexed", chunks=len(chunks)))
    for index, text in enumerate(chunks):
        db.add(DocumentChunk(document_id=doc_id, content=text, chunk_index=index))
    db.commit()


def test_chunk_endpoint_returns_neighbors(client, db_session):
    headers = _headers(client)
    ws = _workspace_id(client, db_session, headers)
    _seed_doc(db_session, doc_id="d1", ws=ws)
    resp = client.get("/knowledge/documents/d1/chunks/1?window=1", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 命中块 1 + 邻域 0、2
    assert [c["chunkIndex"] for c in body["chunks"]] == [0, 1, 2]
    assert body["chunks"][1]["content"] == "块B"


def test_chunk_endpoint_hides_others_private(client, db_session):
    """同工作区里别人的私有文档不能借这个端点读到——这是安全边界。"""
    headers = _headers(client)
    ws = _workspace_id(client, db_session, headers)
    _seed_doc(db_session, doc_id="d-priv", ws=ws, visibility="private", user_id="someone-else")
    assert client.get("/knowledge/documents/d-priv/chunks/0", headers=headers).status_code == 404


# ---- 对话导出 ----


def _seed_chat(db, *, chat_id, uid, title="报销问题"):
    from models import Chat, Message

    now = naive_now()
    db.add(Chat(id=chat_id, title=title, user_id=uid, created_at=now))
    db.add(Message(id=f"{chat_id}-m1", content="赔付上限多少", role="user",
                   chat_id=chat_id, seq=1, created_at=now))
    db.add(Message(id=f"{chat_id}-m2", content="是 500 元", role="assistant",
                   chat_id=chat_id, seq=2, created_at=now))
    db.commit()


def test_export_markdown(client, db_session):
    headers = _headers(client)
    _seed_chat(db_session, chat_id="c1", uid=_uid(db_session))
    resp = client.get("/chats/c1/export?format=md", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers.get("content-disposition", "")
    text = resp.text
    assert "# 报销问题" in text and "### 用户" in text and "赔付上限多少" in text


def test_export_json_and_errors(client, db_session):
    headers = _headers(client)
    _seed_chat(db_session, chat_id="c1", uid=_uid(db_session))
    body = client.get("/chats/c1/export?format=json", headers=headers).json()
    assert body["title"] == "报销问题" and len(body["messages"]) == 2

    assert client.get("/chats/c1/export?format=xml", headers=headers).status_code == 400
    # 别人的对话导不了
    from models import Chat

    db_session.add(Chat(id="cX", title="x", user_id="someone-else", created_at=naive_now()))
    db_session.commit()
    assert client.get("/chats/cX/export", headers=headers).status_code == 404

