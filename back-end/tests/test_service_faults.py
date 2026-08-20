"""服务层故障自愈场景测试（H）。

Agent 链路的每一段都可能挂：检索、流式、兜底、工具。故障是常态，自愈的
含义是"挂了不把整个回合带走"——已有输出不重放、能降级检索就降级、能走
非流式兜底就走兜底，全挂才把错误抛给用户。这里把各段故障逐个端到端验证。
"""
from __future__ import annotations

from datetime import timedelta

from conftest import FakeKnowledgeService, collect, run
from config import settings
from services.chat_service import ChatService

from tests.test_sse_contract import make_service


def test_prefetch_failure_degrades_to_plain_answer(db, monkeypatch):
    """embedding/检索服务挂掉时，预检索以 tool_result(status=error) 呈现且
    不影响主流程：上下文里没有检索内容、没有引用，模型照样回答。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    monkeypatch.setattr(settings, "RAG_CONDENSE_QUERY", False)

    service, adapter = make_service(
        [{"text": "知识库暂时不可用，我用已有信息回答"}],
        FakeKnowledgeService(search_fails=True),
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    pairs = [
        (event["type"], event.get("tool"), event.get("status"))
        for event in events
        if event["type"] in ("tool_start", "tool_result")
    ]
    assert pairs == [
        ("tool_start", "search_knowledge_base", None),
        ("tool_result", "search_knowledge_base", "error"),
    ]
    assert [event["type"] for event in events if event["type"] not in ("tool_start", "tool_result")] == [
        "message_delta"
    ]
    assert all(event["type"] != "error" for event in events)
    # 主模型请求里没有检索注入（系统提示词本身会讲解 fence 格式，不能按字面查）
    user_messages = [
        message
        for message in adapter.calls[0]["messages"]
        if message["role"] == "user"
    ]
    assert user_messages and user_messages[-1]["content"] == "q"
    assert all(event["type"] != "citations" for event in events)


def test_stream_failure_after_text_keeps_partial_answer(db):
    """回答说到一半断流：已流出的文本就是用户看到的全部，不得重来。"""
    service, adapter = make_service([{"text": "已经说了一半", "raise_after_text": True}])

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=False))
    )

    deltas = [event for event in events if event["type"] == "message_delta"]
    assert [event["content"] for event in deltas] == ["已经说了一半"]
    assert all(event["type"] != "error" for event in events)
    assert len(adapter.calls) == 1  # 没有重试这一轮


def test_stream_failure_without_text_falls_back_to_complete(db, monkeypatch):
    """一行都没流出来就断：走非流式兜底，兜底回答正常给到用户。"""
    service, adapter = make_service(
        [{"raise": True}, {"text": "兜底回答"}]
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=False))
    )

    deltas = [event for event in events if event["type"] == "message_delta"]
    assert [event["content"] for event in deltas] == ["兜底回答"]
    assert all(event["type"] != "error" for event in events)
    # 流式一次 + 非流式兜底一次
    assert len(adapter.calls) == 2


def test_stream_and_fallback_both_fail_yields_error(db):
    """流式和兜底全挂：把可见的错误抛给用户，而不是静默吞掉。"""
    service, _adapter = make_service(
        [{"raise": True}, {"raise": True}]
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=False))
    )

    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert "模型调用失败" in errors[0]["error"]


def test_condense_failure_keeps_original_query(db_real, monkeypatch):
    """追问改写（condense）的模型调用挂了：回退原文检索，改写失败不致命。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    monkeypatch.setattr(settings, "RAG_CONDENSE_QUERY", True)
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", False)

    from models import Chat, Message
    from services.clock import naive_now

    now = naive_now()
    db_real.add(Chat(id="c1", user_id="u1", title="测试", created_at=now, updated_at=now))
    for index in range(2):
        db_real.add(
            Message(
                id=f"h{index}",
                chat_id="c1",
                role="user" if index % 2 == 0 else "assistant",
                content="早前的问题",
                created_at=now + timedelta(seconds=index),
            )
        )
    db_real.commit()

    from tests.test_sse_contract import PurposeAwareAdapter

    class BrokenCondenseAdapter(PurposeAwareAdapter):
        """只坏改写（purpose=query_condense），主循环与检索不受影响。"""

        def __init__(self, rounds) -> None:
            super().__init__(rounds)
            self.condense_calls = 0

        async def complete(
            self, *, messages, tools, model, temperature=0.7, max_tokens=2048, top_p=1.0, purpose="chat"
        ):
            if purpose == "query_condense":
                self.condense_calls += 1
                raise RuntimeError("condense model down")
            return await super().complete(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                purpose=purpose,
            )

    adapter = BrokenCondenseAdapter([{"text": "答案"}])
    service = ChatService(model_adapter=adapter)
    service._knowledge_service = FakeKnowledgeService(context="预算文档内容")

    events = run(
        collect(service.stream_ai_response(db_real, "u1", "c1", "q", use_rag=True))
    )

    assert adapter.condense_calls == 1
    assert any(event["type"] == "message_delta" for event in events)
    assert any(
        event["type"] == "tool_result" and event.get("status") == "ok"
        for event in events
    )
    assert all(event["type"] != "error" for event in events)
