"""Agent 循环的行为测试。

关注点是编排：给定模型每轮的行为，循环走了几轮、每轮拿到哪些工具、
工具结果怎么回灌、以及在什么条件下收敛。模型与知识库均为替身，不触网。
"""
from __future__ import annotations

from typing import Any

from conftest import (
    FakeKnowledgeService,
    ScriptedAdapter,
    collect,
    run,
)
from config import settings
from services.chat_service import ChatService


def make_service(
    rounds: list[dict[str, Any]],
    knowledge: FakeKnowledgeService | None = None,
) -> tuple[ChatService, ScriptedAdapter]:
    adapter = ScriptedAdapter(rounds)
    service = ChatService(model_adapter=adapter)
    # 直接塞入缓存字段，避免构造真实 KnowledgeService(会创建 embedding 客户端)
    service._knowledge_service = knowledge or FakeKnowledgeService()
    return service, adapter


def test_plain_chat_runs_one_round_without_tools(db):
    service, adapter = make_service([{"text": "你好"}])

    events = run(collect(service.stream_ai_response(db, "u1", "c1", "hi")))

    assert [event["type"] for event in events] == ["message_delta"]
    assert events[0]["content"] == "你好"
    assert adapter.rounds_used == 1
    assert adapter.calls[0]["tools"] == []


def test_generate_ai_response_concatenates_deltas(db):
    service, _adapter = make_service([{"text": "abc"}])

    assert run(service.generate_ai_response(db, "u1", "c1", "q")) == "abc"


def test_agent_loops_until_model_stops_calling_tools(db, monkeypatch):
    """三个工具串成一次真实的多轮推理：列目录 -> 检索 -> 定向读取 -> 作答。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    knowledge = FakeKnowledgeService(
        context="【参考 1】来源: notes.md，document_id: d1，分块: 3，相关度: 0.8\n正文",
        documents=[{"id": "d1", "name": "notes.md", "chunks": 5, "status": "indexed"}],
    )
    service, adapter = make_service(
        [
            {"tool_calls": [("list_knowledge_documents", {})]},
            {"tool_calls": [("search_knowledge_base", {"query": "预算"})]},
            {
                "tool_calls": [
                    ("read_document_chunk", {"document_id": "d1", "chunk_index": 3})
                ]
            },
            {"text": "根据 notes.md，答案是……"},
        ],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "预算多少", use_rag=True))
    )

    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "message_delta",
    ]
    starts = [event for event in events if event["type"] == "tool_start"]
    assert [event["tool"] for event in starts] == [
        "list_knowledge_documents",
        "search_knowledge_base",
        "read_document_chunk",
    ]
    assert [event["round"] for event in starts] == [1, 2, 3]
    assert adapter.rounds_used == 4
    # 每轮工具结果都以 role=tool 回灌，成为下一轮的输入
    final_messages = adapter.calls[-1]["messages"]
    assert [message["role"] for message in final_messages].count("tool") == 3


def test_round_limit_disables_tools_and_terminates(db, monkeypatch):
    """轮次用尽时最后一轮不再下发工具，循环必然收敛而不是无限调用。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_ROUNDS", 3)
    service, adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"tool_calls": [("search_knowledge_base", {"query": "b"})]},
            {"text": "只能凭现有信息回答"},
        ],
        FakeKnowledgeService(context="ctx"),
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    assert adapter.rounds_used == 3
    assert adapter.calls[0]["tools"] and adapter.calls[1]["tools"]
    assert adapter.calls[2]["tools"] == []
    ended = [event for event in events if event["type"] == "tool_rounds_ended"]
    assert ended and ended[0]["rounds"] == 2
    assert events[-1]["type"] == "message_delta"


def test_unavailable_tool_forces_final_round(db, monkeypatch):
    """工具故障时重试无意义，本轮全失败就直接收敛到最终回答。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"text": "检索不可用，我先用已知信息回答"},
        ],
        FakeKnowledgeService(search_fails=True),
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    results = [event for event in events if event["type"] == "tool_result"]
    assert [event["status"] for event in results] == ["unavailable"]
    assert adapter.rounds_used == 2
    assert adapter.calls[1]["tools"] == []


def test_unknown_tool_is_invalid_arguments_and_loop_continues(db, monkeypatch):
    """模型叫错工具名属于"模型的错"，回灌错误让它下一轮自行修正。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, adapter = make_service(
        [
            {"tool_calls": [("web_search", {"q": "x"})]},
            {"tool_calls": [("search_knowledge_base", {"query": "x"})]},
            {"text": "答案"},
        ],
        FakeKnowledgeService(context="ctx"),
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    statuses = [event["status"] for event in events if event["type"] == "tool_result"]
    assert statuses == ["invalid_arguments", "ok"]
    assert adapter.calls[1]["tools"], "参数错误不应提前终止循环"
    error_message = next(
        message
        for message in adapter.calls[1]["messages"]
        if message["role"] == "tool"
    )
    assert "未注册的工具" in error_message["content"]


def test_tool_result_budget_truncates_and_forces_final(db, monkeypatch):
    """多轮累积的工具结果必须受预算约束，否则几轮就撑爆上下文窗口。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 20)
    monkeypatch.setattr(settings, "TOOL_RESULT_TOTAL_CHARS", 20)
    service, adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"text": "答案"},
        ],
        FakeKnowledgeService(context="x" * 500),
    )

    run(collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True)))

    tool_message = next(
        message
        for message in adapter.calls[1]["messages"]
        if message["role"] == "tool"
    )
    assert "已截断" in tool_message["content"]
    assert adapter.calls[1]["tools"] == []


def test_prefetch_injects_context_and_adjusts_system_prompt(db, monkeypatch):
    """预检索开启时，系统提示词必须告知模型别重复检索，否则就是双通道重复注入。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    knowledge = FakeKnowledgeService(context="【参考 1】来源: notes.md")
    service, adapter = make_service([{"text": "答案"}], knowledge)

    run(collect(service.stream_ai_response(db, "u1", "c1", "预算多少", use_rag=True)))

    messages = adapter.calls[0]["messages"]
    assert "已预先从本地知识库检索" in messages[-1]["content"]
    assert "预算多少" in messages[-1]["content"]
    assert "不要重复检索" in messages[0]["content"]
    assert knowledge.search_queries == ["预算多少"]


def test_prefetch_disabled_leaves_prompt_untouched(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    knowledge = FakeKnowledgeService(context="ctx")
    service, adapter = make_service([{"text": "答案"}], knowledge)

    run(collect(service.stream_ai_response(db, "u1", "c1", "预算多少", use_rag=True)))

    messages = adapter.calls[0]["messages"]
    assert messages[-1]["content"] == "预算多少"
    assert knowledge.search_queries == []


def test_prefetch_failure_does_not_break_the_turn(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    service, adapter = make_service(
        [{"text": "答案"}], FakeKnowledgeService(search_fails=True)
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    assert [event["type"] for event in events] == ["message_delta"]
    assert adapter.calls[0]["messages"][-1]["content"] == "q"


def test_text_tool_protocol_feeds_results_back_as_user_message(db, monkeypatch):
    """GLM 的文本工具协议没有 role=tool，结果要以 user 消息回灌。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, adapter = make_service(
        [
            {
                "text": '<function=call><invoke name="search_knowledge_base">'
                '<parameter name="query">a</parameter></invoke></function>',
                "text_protocol": True,
                "tool_calls": [("search_knowledge_base", {"query": "a"})],
            },
            {"text": "答案"},
        ],
        FakeKnowledgeService(context="ctx"),
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    # 函数标记本身绝不能出现在用户可见的事件流里
    assert all("<function" not in event.get("content", "") for event in events)
    messages = adapter.calls[1]["messages"]
    assert "tool" not in [message["role"] for message in messages]
    assert "工具 search_knowledge_base 的结果" in messages[-1]["content"]


def test_stream_failure_falls_back_to_non_streaming(db):
    service, _adapter = make_service([{"raise": True}, {"text": "兜底回答"}])

    events = run(collect(service.stream_ai_response(db, "u1", "c1", "q")))

    assert [event["type"] for event in events] == ["message_delta"]
    assert events[0]["content"] == "兜底回答"


def test_empty_model_output_surfaces_error(db):
    """空输出不能被当成一条成功的 assistant 消息落库。"""
    service, _adapter = make_service([{"text": ""}])

    events = run(collect(service.stream_ai_response(db, "u1", "c1", "q")))

    assert events == [
        {"type": "error", "error": "模型未返回最终回答，请稍后重试。"}
    ]


def test_protocol_error_is_surfaced(db):
    service, _adapter = make_service([{"protocol_error": True}])

    events = run(collect(service.stream_ai_response(db, "u1", "c1", "q")))

    assert events[0]["type"] == "error"
    assert "工具调用格式" in events[0]["error"]


def test_prefetch_citations_are_emitted(db, monkeypatch):
    """预检索命中的引用要作为结构化事件回传，前端才能渲染来源。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    citations = [
        {
            "document_id": "d1",
            "document_name": "notes.md",
            "chunk_index": 3,
            "chunk_range": [2, 4],
            "score": 0.81,
            "channels": ["dense", "sparse"],
        }
    ]
    knowledge = FakeKnowledgeService(context="【参考 1】...", citations=citations)
    service, _adapter = make_service([{"text": "答案"}], knowledge)

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    citation_events = [event for event in events if event["type"] == "citations"]
    assert citation_events and citation_events[0]["items"] == citations


def test_tool_citations_are_emitted_after_each_search(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    citations = [{"document_id": "d1", "document_name": "notes.md", "chunk_index": 0}]
    knowledge = FakeKnowledgeService(context="ctx", citations=citations)
    service, _adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"text": "答案"},
        ],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    types = [event["type"] for event in events]
    # 引用紧跟在 tool_result 之后发出
    assert types.index("citations") == types.index("tool_result") + 1


def test_tools_without_hits_emit_no_citations(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    knowledge = FakeKnowledgeService(context="", citations=[])
    service, _adapter = make_service(
        [
            {"tool_calls": [("list_knowledge_documents", {})]},
            {"text": "答案"},
        ],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    assert all(event["type"] != "citations" for event in events)

