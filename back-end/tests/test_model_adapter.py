"""OpenAICompatibleAdapter 的流式装配与文本工具协议处理。

提供商把一次工具调用拆成多个 delta 下发，这里验证按 index 归并的正确性；
同时验证 GLM 风格的 <function=call> 文本协议永远不会泄漏到用户可见的文本里。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import run
from services.model_adapter import OpenAICompatibleAdapter


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.request: dict | None = None

    async def create(self, **kwargs):
        self.request = kwargs
        chunks = self._chunks

        class _Stream:
            def __aiter__(self):
                async def generate():
                    for chunk in chunks:
                        yield chunk

                return generate()

        return _Stream()


def _adapter(chunks):
    adapter = OpenAICompatibleAdapter()
    completions = _FakeCompletions(chunks)
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return adapter, completions


async def _drain(adapter, tools=None):
    texts: list[str] = []
    completion = None
    async for chunk in adapter.stream_completion(
        messages=[{"role": "user", "content": "q"}],
        tools=tools or [],
        model="test-model",
    ):
        if chunk.completion is not None:
            completion = chunk.completion
        elif chunk.text is not None:
            texts.append(chunk.text)
    return texts, completion


@pytest.mark.parametrize(
    ("buffer", "withheld"),
    [
        ("", True),
        ("   ", True),
        ("<", True),
        ("<fun", True),
        ("<function=call>", True),
        ("好的，", False),
        ("<html>", False),
        ("# 标题", False),
    ],
)
def test_may_be_text_tool_call(buffer, withheld):
    assert OpenAICompatibleAdapter._may_be_text_tool_call(buffer) is withheld


def test_streams_plain_text_token_by_token():
    adapter, completions = _adapter([_chunk("你"), _chunk("好"), _chunk("")])

    texts, completion = run(_drain(adapter))

    assert texts == ["你", "好"]
    assert completion.content == "你好"
    assert completion.tool_calls == []
    assert completion.streamed_length == 2
    assert completions.request["stream"] is True
    assert "tools" not in completions.request


def test_assembles_tool_call_split_across_deltas():
    """id/name 只出现在首片，arguments 逐片拼接。"""
    adapter, completions = _adapter(
        [
            _chunk(tool_calls=[_tool_delta(0, "call_1", "search_knowledge_base", '{"qu')]),
            _chunk(tool_calls=[_tool_delta(0, None, None, 'ery": "预')]),
            _chunk(tool_calls=[_tool_delta(0, None, None, '算"}')]),
        ]
    )
    tools = [{"type": "function", "function": {"name": "search_knowledge_base"}}]

    texts, completion = run(_drain(adapter, tools))

    assert texts == []
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "search_knowledge_base"
    assert call.arguments == '{"query": "预算"}'
    assert completions.request["tool_choice"] == "auto"


def test_assembles_parallel_tool_calls_by_index():
    adapter, _completions = _adapter(
        [
            _chunk(
                tool_calls=[
                    _tool_delta(0, "call_a", "list_knowledge_documents", "{}"),
                    _tool_delta(1, "call_b", "search_knowledge_base", '{"query"'),
                ]
            ),
            _chunk(tool_calls=[_tool_delta(1, None, None, ': "x"}')]),
        ]
    )

    _texts, completion = run(_drain(adapter))

    assert [call.name for call in completion.tool_calls] == [
        "list_knowledge_documents",
        "search_knowledge_base",
    ]
    assert completion.tool_calls[1].arguments == '{"query": "x"}'


def test_text_protocol_content_is_never_streamed():
    """整段都是函数标记时，一个字符都不能透出给用户。"""
    adapter, _completions = _adapter(
        [
            _chunk("<func"),
            _chunk('tion=call><invoke name="search_knowledge_base">'),
            _chunk('<parameter name="query">预算</parameter></invoke></function>'),
        ]
    )

    texts, completion = run(_drain(adapter))

    assert texts == []
    assert completion.uses_text_tool_protocol is True
    assert completion.content == ""
    assert [call.name for call in completion.tool_calls] == ["search_knowledge_base"]
    assert completion.tool_calls[0].arguments == '{"query": "预算"}'


def test_prose_then_function_markup_is_a_protocol_error():
    """散文 + 函数标记的混合响应无法安全执行，应作为协议错误上报。"""
    adapter, _completions = _adapter(
        [
            _chunk("好的，"),
            _chunk('<function=call><invoke name="x"></invoke></function>'),
        ]
    )

    texts, completion = run(_drain(adapter))

    assert texts == ["好的，"]
    assert completion.protocol_error == "模型返回了无法解析的工具调用格式"
    assert completion.tool_calls == []
