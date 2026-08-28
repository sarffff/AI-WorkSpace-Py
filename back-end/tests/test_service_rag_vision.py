"""服务层场景 A（RAG 追问）/ I（工作区作用域）/ J（视觉）测试。

单元层已覆盖改写失败回退（H 场景）、上下文预算（SSE 契约）、视觉引用收集。
这里补服务层正向链路的三个断言：改写结果真的进了预检索、检索永远按工作区
作用域进行、图片在支持视觉的模型上变成内容块。
"""
from __future__ import annotations

from datetime import timedelta

from conftest import FakeKnowledgeService, collect, run
from config import settings
from models import Chat, Message, User
from services import workspace_service
from services.clock import naive_now

from tests.test_sse_contract import make_service


def _seed_history(db_real, *, count: int = 2) -> None:
    now = naive_now()
    db_real.add(Chat(id="c1", user_id="u1", title="测试", created_at=now, updated_at=now))
    for index in range(count):
        db_real.add(
            Message(
                id=f"h{index}",
                chat_id="c1",
                role="user" if index % 2 == 0 else "assistant",
                content="早前的问题与回答",
                created_at=now + timedelta(seconds=index),
            )
        )
    db_real.commit()


# ========== A：追问改写 ==========


def test_condense_success_rewrites_prefetch_query(db_real, monkeypatch):
    """有历史 + 改写开启：预检索用的必须是改写后的自包含问题，不是原文追问。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    monkeypatch.setattr(settings, "RAG_CONDENSE_QUERY", True)
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", False)
    _seed_history(db_real)

    knowledge = FakeKnowledgeService(
        context="参考内容：赔偿标准按合同执行",
        citations=[{"document_id": "d1", "chunk_index": 0, "relevance": 0.9}],
    )
    service, adapter = make_service(
        [{"text": "改写后的赔偿标准"}, {"text": "答案"}],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db_real, "u1", "c1", "那赔偿标准呢", use_rag=True))
    )

    starts = [event for event in events if event["type"] == "tool_start"]
    assert starts[0]["input"] == {"query": "改写后的赔偿标准"}
    assert knowledge.search_queries == ["改写后的赔偿标准"]
    citations = [event for event in events if event["type"] == "citations"]
    assert len(citations) == 1
    assert citations[0]["items"][0]["document_id"] == "d1"
    user_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "user"
    ]
    assert "参考内容：赔偿标准按合同执行" in user_messages[-1]["content"]


# ========== I：工作区作用域 ==========


def test_knowledge_search_scoped_to_workspace_id(db_real, monkeypatch):
    """知识库按工作区共享：工具检索用的作用域是 workspace_id，不是用户 id。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)

    user = User(email="u1@example.com", username="u1", role="admin")
    db_real.add(user)
    db_real.commit()
    db_real.refresh(user)
    workspace = workspace_service.resolve_for_user(db_real, user)
    assert workspace.id != user.id

    class ScopedKnowledge(FakeKnowledgeService):
        def __init__(self) -> None:
            super().__init__(context="预算文档内容")
            self.scope_ids: list = []

        async def build_rag_context_with_citations(
            self, db, query, workspace_id, top_k=5, viewer_id=None
        ):
            self.scope_ids.append(workspace_id)
            self.viewer_ids.append(viewer_id)
            return self.context, []

    knowledge = ScopedKnowledge()
    service, adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "预算"})]},
            {"text": "答案"},
        ],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db_real, str(user.id), "c1", "预算多少", use_rag=True))
    )

    assert knowledge.scope_ids == [workspace.id]
    assert any(event["type"] == "message_delta" for event in events)
    tool_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "tool"
    ]
    assert "预算文档内容" in tool_messages[-1]["content"]


# ========== J：视觉 ==========


def _write_test_image(upload_root):
    """写一张最小的合法 PNG（1x1）。"""
    import base64

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
    )
    (upload_root / "202608").mkdir(parents=True, exist_ok=True)
    (upload_root / "202608" / "chart.png").write_bytes(base64.b64decode(png_b64))


def test_vision_images_become_content_blocks(db, monkeypatch):
    """模型在视觉白名单里：消息里的图片引用变成 image_url 内容块，文本里留序号。

    ``LLM_MODEL`` 必须一起钉死。``chat_service`` 走的是
    ``model or settings.LLM_MODEL``，测试没传模型，于是实际参与白名单比对的是
    **本地 .env 里的模型名**。只钉 ``VISION_MODELS`` 的话，这条测试断言的其实是
    "开发机的 LLM_MODEL 恰好等于 glm-4.5-air"——这条测试因此长期为红：.env 里
    是 ``glm-4.6v``，白名单写的是 ``glm-4.5-air``，``supports_vision`` 正确地
    返回 False，而报错看起来像视觉功能坏了。
    """
    from pathlib import Path
    from tempfile import TemporaryDirectory

    td = TemporaryDirectory()
    monkeypatch.setattr(settings, "UPLOAD_DIR", td.name)
    _write_test_image(Path(td.name))
    monkeypatch.setattr(settings, "LLM_MODEL", "glm-4.5-air")
    monkeypatch.setattr(settings, "VISION_MODELS", "glm-4.5-air")

    service, adapter = make_service([{"text": "答案"}])

    prompt = "帮我看看这张图 ![图表](/uploads/202608/chart.png)"
    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", prompt, use_rag=False))
    )

    user_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "user"
    ]
    content = user_messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "[图片 1：图表]" in content[0]["text"]
    image_block = content[1]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
    assert any(event["type"] == "message_delta" for event in events)
    td.cleanup()


def test_non_vision_model_keeps_url_in_text(db, monkeypatch):
    """模型不在白名单：图片引用原样留在文本里（模型至少能说自己收到了链接）。

    两个都钉：这条现在是靠"本地 LLM_MODEL 恰好不等于 glm-4v-plus"才通过的，
    谁把 .env 换成那个名字它就跟着红——和上面那条是同一个坑的反面。
    """
    monkeypatch.setattr(settings, "LLM_MODEL", "glm-4.5-air")
    monkeypatch.setattr(settings, "VISION_MODELS", "glm-4v-plus")

    service, adapter = make_service([{"text": "答案"}])

    prompt = "看图 ![图表](/uploads/202608/chart.png)"
    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", prompt, use_rag=False))
    )

    user_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "user"
    ]
    content = user_messages[-1]["content"]
    assert isinstance(content, str)
    assert "/uploads/202608/chart.png" in content
    assert any(event["type"] == "message_delta" for event in events)