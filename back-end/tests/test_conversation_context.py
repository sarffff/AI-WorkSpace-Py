"""对话历史的 token 预算裁剪与滚动摘要。"""
from __future__ import annotations

from typing import Any

import pytest

from config import settings
from services import conversation_context as cc
from services.conversation_context import ConversationContextBuilder
from services.model_adapter import ModelCompletion
from services.token_budget import HeuristicTokenCounter, HistoryMessage

from conftest import run

COUNTER = HeuristicTokenCounter()


class FakeSummaryStore:
    """进程内摘要缓存替身，避免测试碰到真实 Redis。"""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def get(self, chat_id: str) -> dict[str, Any] | None:
        return self.data.get(chat_id)

    def set(self, chat_id: str, value: dict[str, Any]) -> None:
        self.data[chat_id] = value


class CountingAdapter:
    """记录每次摘要调用的提示词，便于断言滚动行为。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.prompts: list[str] = []

    async def complete(self, *, messages, tools, model, **kwargs) -> ModelCompletion:
        self.prompts.append(messages[-1]["content"])
        content = self._responses.pop(0) if self._responses else "摘要内容"
        return ModelCompletion(content=content, tool_calls=[])


class BrokenAdapter:
    async def complete(self, **kwargs) -> ModelCompletion:
        raise RuntimeError("model down")


def _history(count: int, filler: str = "x" * 200) -> list[HistoryMessage]:
    return [
        HistoryMessage(f"m{index}", "user" if index % 2 == 0 else "assistant", filler)
        for index in range(count)
    ]


@pytest.fixture(autouse=True)
def store(monkeypatch) -> FakeSummaryStore:
    fake = FakeSummaryStore()
    monkeypatch.setattr(cc, "summary_store", fake)
    monkeypatch.setattr(settings, "HISTORY_SUMMARY", True)
    return fake


def _builder(adapter) -> ConversationContextBuilder:
    return ConversationContextBuilder(adapter, counter=COUNTER)


def test_short_history_passes_through_untouched(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 100_000)
    adapter = CountingAdapter()

    context = run(_builder(adapter).build("c1", _history(4)))

    assert len(context.messages) == 4
    assert context.summarized == 0
    assert not context.compacted
    assert adapter.prompts == []  # 没溢出就不该花钱做摘要


def test_overflow_inserts_a_summary_system_message(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)
    adapter = CountingAdapter(["用户在讨论预算审批"])

    context = run(_builder(adapter).build("c1", _history(6)))

    assert context.compacted
    assert context.messages[0]["role"] == "system"
    assert "更早对话的摘要" in context.messages[0]["content"]
    assert "用户在讨论预算审批" in context.messages[0]["content"]
    assert context.kept + context.summarized == 6


def test_summary_is_reused_when_dropped_set_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)
    adapter = CountingAdapter(["第一次摘要"])
    builder = _builder(adapter)
    history = _history(6)

    first = run(builder.build("c1", history))
    second = run(builder.build("c1", history))

    assert len(adapter.prompts) == 1  # 同一批历史不重复摘要
    assert first.messages[0]["content"] == second.messages[0]["content"]


def test_summary_rolls_forward_on_top_of_the_previous_one(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)
    adapter = CountingAdapter(["旧摘要", "新摘要"])
    builder = _builder(adapter)

    run(builder.build("c1", _history(6)))
    context = run(builder.build("c1", _history(10)))

    assert len(adapter.prompts) == 2
    # 第二次只把新掉出窗口的消息交给模型，并带上已有摘要
    assert "[已有摘要]" in adapter.prompts[1]
    assert "旧摘要" in adapter.prompts[1]
    assert "新摘要" in context.messages[0]["content"]


def test_stale_summary_is_discarded_when_history_shrinks(monkeypatch, store):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)
    adapter = CountingAdapter(["旧摘要", "重算摘要"])
    builder = _builder(adapter)

    run(builder.build("c1", _history(10)))
    # 模拟编辑/重新生成删掉了 dropped 区间里的消息:dropped 集合既变了也
    # 变少,缓存里记录的 count(8)超过新的 dropped 规模,旧摘要不再可信,
    # 必须丢弃后从头重算,而不是把它当增量基础
    context = run(builder.build("c1", _history(6)))

    assert "[已有摘要]" not in adapter.prompts[-1]
    assert "重算摘要" in context.messages[0]["content"]


def test_summary_disabled_falls_back_to_plain_window(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)
    monkeypatch.setattr(settings, "HISTORY_SUMMARY", False)
    adapter = CountingAdapter()

    context = run(_builder(adapter).build("c1", _history(6)))

    assert adapter.prompts == []
    assert context.summarized == 0
    assert all(message["role"] != "system" for message in context.messages)


def test_summarization_failure_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(settings, "HISTORY_TOKEN_BUDGET", 120)

    context = run(_builder(BrokenAdapter()).build("c1", _history(6)))

    # 摘要失败不能中断对话，只是丢掉早期上下文
    assert all(message["role"] != "system" for message in context.messages)
    assert context.messages


def test_empty_history_produces_empty_context():
    context = run(_builder(CountingAdapter()).build("c1", []))

    assert context.messages == []
    assert not context.compacted
