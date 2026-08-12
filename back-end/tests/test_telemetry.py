"""埋点基础设施：span 树、上下文传播、无 trace 时的空操作。"""
from __future__ import annotations

import asyncio

import pytest

from config import settings
from services import telemetry as tel
from services.telemetry import NoopSpan, SpanKind, TokenSource, Tracer, set_span_defaults

from conftest import run


@pytest.fixture
def tracer(monkeypatch) -> Tracer:
    """禁掉落库，测试只关心内存里的 span 树是否正确。"""
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(tel.Tracer, "_flush", lambda self, trace: _noop())
    return Tracer()


async def _noop() -> None:
    return None


def _by_name(trace, name: str):
    return next(span for span in trace.spans if span.name == name)


def test_nested_spans_build_a_tree(tracer):
    async def scenario():
        async with tracer.trace(user_id="u1", chat_id="c1") as trace:
            async with tracer.span("chat.turn", SpanKind.AGENT):
                async with tracer.span("llm.chat", SpanKind.LLM, model="m1"):
                    pass
                async with tracer.span("tool.search", SpanKind.TOOL):
                    async with tracer.span("retrieval.hybrid", SpanKind.RETRIEVAL):
                        pass
        return trace

    trace = run(scenario())

    turn = _by_name(trace, "chat.turn")
    assert turn.parent_id is None
    assert _by_name(trace, "llm.chat").parent_id == turn.span_id
    tool = _by_name(trace, "tool.search")
    assert tool.parent_id == turn.span_id
    assert _by_name(trace, "retrieval.hybrid").parent_id == tool.span_id
    assert all(span.trace_id == trace.trace_id for span in trace.spans)


def test_sibling_spans_share_a_parent(tracer):
    async def scenario():
        async with tracer.trace() as trace:
            async with tracer.span("chat.turn", SpanKind.AGENT):
                for _ in range(3):
                    async with tracer.span("llm.chat", SpanKind.LLM):
                        pass
        return trace

    trace = run(scenario())
    parent = _by_name(trace, "chat.turn").span_id
    llm_spans = [span for span in trace.spans if span.name == "llm.chat"]
    assert len(llm_spans) == 3
    assert {span.parent_id for span in llm_spans} == {parent}


def test_span_records_duration_and_usage(tracer):
    async def scenario():
        async with tracer.trace() as trace:
            async with tracer.span("llm.chat", SpanKind.LLM) as span:
                await asyncio.sleep(0.01)
                span.set_usage(
                    prompt_tokens=120,
                    completion_tokens=30,
                    source=TokenSource.PROVIDER,
                    model="glm-4.5-air",
                )
        return trace

    trace = run(scenario())
    span = trace.spans[0]
    assert span.duration_ms is not None and span.duration_ms >= 5
    assert (span.prompt_tokens, span.completion_tokens) == (120, 30)
    assert span.token_source == "provider"
    assert span.model == "glm-4.5-air"


def test_exception_marks_span_failed_and_propagates(tracer):
    async def scenario():
        async with tracer.trace() as trace:
            with pytest.raises(ValueError):
                async with tracer.span("tool.x", SpanKind.TOOL):
                    raise ValueError("boom")
        return trace

    trace = run(scenario())
    assert trace.spans[0].status == "error"
    assert trace.spans[0].error_type == "ValueError"


def test_cancellation_is_not_counted_as_error(tracer):
    """用户中断流式生成不是故障，混进 error 会污染错误率。"""

    async def scenario():
        async with tracer.trace() as trace:
            with pytest.raises(asyncio.CancelledError):
                async with tracer.span("llm.chat", SpanKind.LLM):
                    raise asyncio.CancelledError()
        return trace

    trace = run(scenario())
    assert trace.spans[0].status == "cancelled"


def test_span_defaults_apply_to_later_spans(tracer):
    """轮次由 set_span_defaults 传播，不需要把参数塞进适配器签名。"""

    async def scenario():
        async with tracer.trace() as trace:
            set_span_defaults(round=1)
            async with tracer.span("llm.chat", SpanKind.LLM):
                pass
            set_span_defaults(round=2)
            async with tracer.span("llm.chat", SpanKind.LLM):
                async with tracer.span("retrieval.hybrid", SpanKind.RETRIEVAL):
                    pass
        return trace

    trace = run(scenario())
    rounds = [span.attributes.get("round") for span in trace.spans]
    assert rounds == [1, 2, 2]


def test_explicit_attributes_win_over_defaults(tracer):
    async def scenario():
        async with tracer.trace() as trace:
            set_span_defaults(round=1)
            async with tracer.span("llm.chat", SpanKind.LLM, round=9):
                pass
        return trace

    trace = run(scenario())
    assert trace.spans[0].attributes["round"] == 9


def test_set_ignores_none_values(tracer):
    async def scenario():
        async with tracer.trace() as trace:
            async with tracer.span("llm.chat", SpanKind.LLM) as span:
                span.set(model=None, tools=None, hits=0)
        return trace

    trace = run(scenario())
    assert "model" not in trace.spans[0].attributes
    assert trace.spans[0].attributes["hits"] == 0


def test_span_outside_trace_is_a_noop(tracer):
    """service 被直接调用（eval 脚本、单测）时不该因为没开 trace 而报错。"""

    async def scenario():
        async with tracer.span("llm.chat", SpanKind.LLM) as span:
            assert isinstance(span, NoopSpan)
            span.set(anything=1)
            span.set_usage(prompt_tokens=1, completion_tokens=1)
            span.fail(ValueError("ignored"))
        return True

    assert run(scenario()) is True


def test_disabled_telemetry_produces_no_spans(monkeypatch):
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", False)
    disabled = Tracer()

    async def scenario():
        async with disabled.trace(user_id="u1") as trace:
            async with disabled.span("chat.turn", SpanKind.AGENT) as span:
                span.set(x=1)
            return trace

    assert run(scenario()) is None


def test_attributes_are_truncated(monkeypatch):
    monkeypatch.setattr(settings, "TELEMETRY_ATTR_MAX_CHARS", 40)
    encoded = tel._encode_attributes({"blob": "x" * 500})
    assert encoded is not None and len(encoded) == 40


def test_empty_attributes_encode_to_none():
    assert tel._encode_attributes({}) is None
