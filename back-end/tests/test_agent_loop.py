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
from services import prompt_library
from services import tool_history
from services.telemetry import tracer


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

    # 预检索以工具事件对外呈现,失败也不中断主流程
    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_result",
        "message_delta",
    ]
    assert events[1]["status"] == "error"
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


# ========== 提示词版本 ==========


def test_prompt_version_override_is_used_verbatim(db, monkeypatch):
    """请求指定的版本必须原样生效，不能被 settings 里的默认版本盖掉。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, adapter = make_service([{"text": "答案"}])

    run(
        collect(
            service.stream_ai_response(
                db, "u1", "c1", "q", use_rag=True, prompt_version="v3-lean"
            )
        )
    )

    expected = prompt_library.get("chat_system_rag", "v3-lean").render(
        flags={"prefetched": False}
    )
    assert adapter.calls[0]["messages"][0]["content"] == expected


def test_prompt_version_falls_back_to_settings(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "PROMPT_CHAT_SYSTEM_VERSION", "v1")
    service, adapter = make_service([{"text": "答案"}])

    run(collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True)))

    system = adapter.calls[0]["messages"][0]["content"]
    # v1 是阶段二的形态，没有定界符声明——用它来确认切换真的生效了
    assert "资料开始" not in system
    assert "search_knowledge_base" in system


def test_turn_span_records_prompt_version(db, monkeypatch):
    """埋点里必须留下版本号，否则回头看 trace 不知道是哪版提示词答的。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, _adapter = make_service([{"text": "答案"}])
    recorded: list[dict[str, Any]] = []

    original = tracer.span

    def spy(name: str, kind: Any, **attributes: Any):
        if name == "chat.turn":
            recorded.append(attributes)
        return original(name, kind, **attributes)

    monkeypatch.setattr(tracer, "span", spy)
    run(
        collect(
            service.stream_ai_response(
                db, "u1", "c1", "q", use_rag=True, prompt_version="v3-lean"
            )
        )
    )

    assert recorded and recorded[0]["prompt_version"] == "chat_system_rag@v3-lean"


def test_plain_chat_ignores_prompt_version(db):
    """关掉 RAG 时用的是另一类提示词，它没有版本开关，传了也不该炸。"""
    service, adapter = make_service([{"text": "你好"}])

    run(
        collect(
            service.stream_ai_response(db, "u1", "c1", "hi", prompt_version="v3-lean")
        )
    )

    expected = prompt_library.get("chat_system_plain").render()
    assert adapter.calls[0]["messages"][0]["content"] == expected


# ========== 工具轨迹（跨回合记忆） ==========


def test_tool_steps_are_persisted(db_real, monkeypatch):
    """回合内的工具执行必须落库。不落的话回合一结束轨迹就没了，
    下一回合模型对自己读过什么毫无记忆。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    citations = [{"document_id": "d1", "document_name": "notes.md", "chunk_index": 3}]
    knowledge = FakeKnowledgeService(context="试用期 6 个月", citations=citations)
    service, _adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "试用期"})]},
            {"text": "6 个月"},
        ],
        knowledge,
    )

    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "试用期多久", use_rag=True, message_id="m1"
            )
        )
    )

    steps = tool_history.load_recent(db_real, "c1")
    assert len(steps) == 1
    assert steps[0].tool_name == "search_knowledge_base"
    assert steps[0].round_index == 1
    assert steps[0].status == "ok"
    assert steps[0].result_content == "试用期 6 个月"
    # 引用一起存下来，下个回合模型才能直接接 read_document_chunk
    assert "d1" in (steps[0].citations or "")


def test_prefetch_is_recorded_as_round_zero(db_real, monkeypatch):
    """预检索发生在模型开口之前，记成第 0 轮而不是第 1 轮——
    否则回头看轨迹会以为模型自己决定检索过一次。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", True)
    service, _adapter = make_service(
        [{"text": "答案"}], FakeKnowledgeService(context="参考内容")
    )

    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "q", use_rag=True, message_id="m1"
            )
        )
    )

    steps = tool_history.load_recent(db_real, "c1")
    assert [step.round_index for step in steps] == [0]
    assert steps[0].tool_name == "search_knowledge_base"


def test_failed_tool_is_recorded_too(db_real, monkeypatch):
    """失败也要留档。抹掉它等于让模型下个回合重走同一条死路。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    service, _adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"text": "先用已知信息回答"},
        ],
        FakeKnowledgeService(search_fails=True),
    )

    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "q", use_rag=True, message_id="m1"
            )
        )
    )

    steps = tool_history.load_recent(db_real, "c1")
    assert [step.status for step in steps] == ["unavailable"]


def _first_turn(db_real, message_id: str = "m1") -> None:
    service, _adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "试用期"})]},
            {"text": "6 个月"},
        ],
        FakeKnowledgeService(context="试用期为 6 个月"),
    )
    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "试用期多久", use_rag=True, message_id=message_id
            )
        )
    )


def test_previous_trajectory_is_injected_before_the_current_question(
    db_real, monkeypatch
):
    """这就是"跨回合失忆"被修掉的地方：第二个回合里模型能看到
    第一个回合检索过什么，不必重新检索一遍或者照着上次的措辞往回编。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    _first_turn(db_real)

    service, adapter = make_service([{"text": "确定"}], FakeKnowledgeService())
    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "确定吗", use_rag=True, message_id="m2"
            )
        )
    )

    messages = adapter.calls[0]["messages"]
    # 轨迹紧贴当前问题：离得越近越不容易被当成更早的对话内容
    assert messages[-1]["content"] == "确定吗"
    assert messages[-2]["role"] == "system"
    assert "search_knowledge_base" in messages[-2]["content"]
    assert "试用期" in messages[-2]["content"]


def test_injected_trajectory_is_not_disguised_as_a_tool_message(db_real, monkeypatch):
    """不能还原成 role=tool：那需要连带伪造对应的 assistant tool_calls 消息，
    而且语义也错——上一回合的结果是"我做过什么"，不是"你刚要的东西在这"。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    _first_turn(db_real)

    service, adapter = make_service([{"text": "确定"}], FakeKnowledgeService())
    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "确定吗", use_rag=True, message_id="m2"
            )
        )
    )

    roles = [message["role"] for message in adapter.calls[0]["messages"]]
    assert "tool" not in roles


def test_regenerating_a_turn_ignores_its_own_earlier_steps(db_real, monkeypatch):
    """同一条用户消息重跑时不能回灌上一次的半截轨迹，
    否则模型以为检索已经做过，直接跳过本该重做的调用。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    _first_turn(db_real, message_id="m1")

    service, adapter = make_service([{"text": "重答"}], FakeKnowledgeService())
    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "试用期多久", use_rag=True, message_id="m1"
            )
        )
    )

    contents = [message["content"] for message in adapter.calls[0]["messages"]]
    assert all("此前的工具执行记录" not in content for content in contents)


def test_disabled_tool_history_restores_the_amnesiac_behaviour(db_real, monkeypatch):
    """关掉开关就该完全回到"每个回合从零开始"，这是对照实验的基准。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", False)
    _first_turn(db_real)

    service, adapter = make_service([{"text": "确定"}], FakeKnowledgeService())
    run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "确定吗", use_rag=True, message_id="m2"
            )
        )
    )

    contents = [message["content"] for message in adapter.calls[0]["messages"]]
    assert all("此前的工具执行记录" not in content for content in contents)
    # 关掉时 load_recent 一律返回空，所以要先打开再确认库里真的什么都没写
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", True)
    assert tool_history.load_recent(db_real, "c1") == []




