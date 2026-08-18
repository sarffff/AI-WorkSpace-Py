"""预检索指代消解:改写成功时替换检索 query,任何失败回退原文。"""
from __future__ import annotations

from config import settings
from services.chat_service import ChatService
from services.model_adapter import ModelCompletion
from services.token_budget import HistoryMessage
from conftest import run


class StubAdapter:
    def __init__(self, content: str | None):
        self._content = content
        self.calls = 0

    async def complete(self, *, messages, tools, model, **kwargs):
        self.calls += 1
        if self._content is None:
            raise RuntimeError("llm down")
        return ModelCompletion(content=self._content, tool_calls=[])


def _history(*roles: str) -> list[HistoryMessage]:
    return [
        HistoryMessage(id=f"m{i}", role=role, content="内容")
        for i, role in enumerate(roles)
    ]


def test_no_history_returns_prompt_without_calling_model():
    adapter = StubAdapter("改写后的问题")
    service = ChatService(model_adapter=adapter)

    result = run(service._condense_query([], "报销流程是什么"))

    assert result == "报销流程是什么"
    assert adapter.calls == 0


def test_condensed_query_used_when_model_returns_single_line():
    adapter = StubAdapter("差旅费的报销流程是什么")
    service = ChatService(model_adapter=adapter)

    result = run(
        service._condense_query(_history("user", "assistant"), "那它的流程呢")
    )

    assert result == "差旅费的报销流程是什么"
    assert adapter.calls == 1


def test_falls_back_to_original_on_model_failure():
    service = ChatService(model_adapter=StubAdapter(None))

    result = run(
        service._condense_query(_history("user"), "那它的流程呢")
    )

    assert result == "那它的流程呢"


def test_falls_back_when_output_looks_like_transcript():
    # 输出比原问题长得多,多半是把整段对话抄了一遍而不是改写
    service = ChatService(model_adapter=StubAdapter("user: 问题\nassistant: 回答" * 30))

    result = run(service._condense_query(_history("user"), "短问题"))

    assert result == "短问题"


def test_disabled_by_config_returns_prompt():
    service = ChatService(model_adapter=StubAdapter("改写"))
    settings.RAG_CONDENSE_QUERY = False
    try:
        result = run(service._condense_query(_history("user"), "原问题"))
    finally:
        settings.RAG_CONDENSE_QUERY = True

    assert result == "原问题"
