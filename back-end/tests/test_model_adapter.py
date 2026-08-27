"""OpenAICompatibleAdapter 的流式装配与文本工具协议处理。

提供商把一次工具调用拆成多个 delta 下发，这里验证按 index 归并的正确性；
同时验证 GLM 风格的 <function=call> 文本协议永远不会泄漏到用户可见的文本里。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings
from conftest import run
from services.model_adapter import OpenAICompatibleAdapter


def _chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


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


# ========== finish_reason ==========
#
# 这个字段是为一类具体故障加的:混合推理模型先花 max_tokens 思考,预算不够时
# 返回空串。没有它,"模型没什么要说"和"预算被思考吃光了"在调用方看来完全同形
# ——两者都是 content 为空,而后者会让整个功能静默失效(实测全库 7 个辅助
# 调用点有 5 个长期落在这里)。


def test_stream_captures_finish_reason():
    """终止原因只出现在最后一个带 choices 的分片上,前面的都是 None。"""
    adapter, _completions = _adapter(
        [_chunk("你"), _chunk("好"), _chunk(None, finish_reason="stop")]
    )

    _texts, completion = run(_drain(adapter))

    assert completion.finish_reason == "stop"
    assert completion.truncated is False


def test_stream_marks_truncation():
    adapter, _completions = _adapter(
        [_chunk("答案是"), _chunk(None, finish_reason="length")]
    )

    _texts, completion = run(_drain(adapter))

    assert completion.finish_reason == "length"
    assert completion.truncated is True


def test_empty_content_with_length_is_reported(caplog):
    """预算全被思考吃掉——正文为空的截断是故障,必须当场喊出来。

    正文非空的截断只是"答长了",属于成本项,记 span 就够;正文为空的那种会让
    调用方走静默降级路径,而那正是这类 bug 藏得住的原因。
    """
    import logging

    adapter, _completions = _adapter([_chunk(None, finish_reason="length")])

    with caplog.at_level(logging.WARNING, logger="model_adapter"):
        _texts, completion = run(_drain(adapter))

    assert completion.content == ""
    assert completion.truncated is True
    assert any("finish_reason=length" in record.message for record in caplog.records)


def test_non_empty_truncation_is_not_warned(caplog):
    import logging

    adapter, _completions = _adapter(
        [_chunk("说到一半"), _chunk(None, finish_reason="length")]
    )

    with caplog.at_level(logging.WARNING, logger="model_adapter"):
        run(_drain(adapter))

    assert not [r for r in caplog.records if "finish_reason=length" in r.message]


def test_missing_finish_reason_stays_none():
    """老端点/非标准实现可能不给这个字段。None 不等于 "stop"——

    "没有截断信息"和"正常结束"是两件事,把缺失当成正常会让排查时误以为
    已经排除了预算问题。
    """
    adapter = OpenAICompatibleAdapter()
    completions = _FakeCompletions([SimpleNamespace(choices=[
        SimpleNamespace(delta=SimpleNamespace(content="你好", tool_calls=None))
    ])])
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    _texts, completion = run(_drain(adapter))

    assert completion.finish_reason is None
    assert completion.truncated is False


# ========== 辅助调用的超时与思考链 ==========
#
# 2026-08-27 加。此前 ``AsyncOpenAI`` 不带 timeout/max_retries 构造，吃 SDK 默认
# **read=600s × max_retries=2 → 最坏一次 1800 秒**。实测 rerank 变体 p90 延迟
# 255 秒、最大 336 秒（baseline 37 秒），eval 里 10/54 的降级就是耗尽重试的那些。


def _bad_request_error() -> Exception:
    """一个会被 ``_is_bad_request`` 认出来的异常。

    判据是 ``status_code == 400`` 或类名叫 ``BadRequestError``，所以这里造
    status_code=400 的普通异常就够，不需要拉起真的 openai 异常类型。
    """
    error = RuntimeError("unknown parameter: thinking")
    error.status_code = 400
    return error


class _RecordingCompletions:
    """记录每次 create 收到的 kwargs，可选地按次序抛异常。"""

    def __init__(self, errors=None):
        self.calls: list[dict] = []
        self._errors = list(errors or [])

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="[1, 2]", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            model="m",
        )


class _RecordingClient:
    def __init__(self, errors=None):
        self.completions = _RecordingCompletions(errors)
        self.chat = SimpleNamespace(completions=self.completions)
        self.options: list[dict] = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


def _adapter_with(client):
    adapter = OpenAICompatibleAdapter.__new__(OpenAICompatibleAdapter)
    adapter._client = client
    return adapter


def _reset_thinking_flag():
    OpenAICompatibleAdapter._thinking_opt_out_supported = True


def test_auxiliary_calls_get_a_shorter_timeout():
    """``purpose != "chat"`` 时收紧超时与重试。

    辅助调用**全都有降级路径**（重排失败退回融合序、HyDE 失败用原查询），
    为一个可以放弃的增强等 600s×3 是纯亏，而且拖慢的是用户正在等的那次回答。
    """
    _reset_thinking_flag()
    client = _RecordingClient()
    adapter = _adapter_with(client)
    run(
        adapter.complete(
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            model="m",
            purpose="rerank",
        )
    )
    assert client.options, "辅助调用该走 with_options"
    assert client.options[0]["timeout"] == settings.LLM_AUXILIARY_TIMEOUT_SECONDS
    assert client.options[0]["max_retries"] == settings.LLM_AUXILIARY_MAX_RETRIES


def test_chat_calls_keep_the_default_timeout():
    """主回答不受这个约束——用户确实在等它，不该为了省时间砍掉重试。"""
    _reset_thinking_flag()
    client = _RecordingClient()
    adapter = _adapter_with(client)
    run(
        adapter.complete(
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            model="m",
            purpose="chat",
        )
    )
    assert client.options == [], "chat 不该收紧超时"


def test_auxiliary_calls_disable_the_reasoning_chain():
    """辅助调用关掉思考链。

    实测 20 候选的 listwise 重排：开思考 20.3 秒 / 1011 输出 token，关掉
    0.4 秒 / 7 token——**快 50 倍**，而且排序更好（开着时那 1011 token 只吐出
    ``[10]``，关掉给出 ``[10, 20]``）。排序不需要思考链。
    """
    _reset_thinking_flag()
    client = _RecordingClient()
    adapter = _adapter_with(client)
    run(
        adapter.complete(
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            model="m",
            purpose="rerank",
        )
    )
    assert client.completions.calls[0].get("extra_body") == {
        "thinking": {"type": "disabled"}
    }


def test_chat_keeps_the_reasoning_chain():
    """主回答的思考链是有价值的，不能顺手关掉。"""
    _reset_thinking_flag()
    client = _RecordingClient()
    adapter = _adapter_with(client)
    run(
        adapter.complete(
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            model="m",
            purpose="chat",
        )
    )
    assert "extra_body" not in client.completions.calls[0]


def test_thinking_opt_out_is_remembered_after_rejection(caplog):
    """端点拒绝这个参数时只回退一次并记住，不是每次都白试一遍。

    和 ``_stream_usage_supported`` 同一个套路。每次都试的代价是每个辅助调用
    都多一次往返。
    """
    _reset_thinking_flag()
    bad_request = _bad_request_error()
    client = _RecordingClient(errors=[bad_request, None, None])
    adapter = _adapter_with(client)
    for _ in range(2):
        run(
            adapter.complete(
                messages=[{"role": "user", "content": "q"}],
                tools=[],
                model="m",
                purpose="rerank",
            )
        )
    assert OpenAICompatibleAdapter._thinking_opt_out_supported is False
    # 第 1 次带参数被拒 → 第 2 次不带（同一次调用内的回退）→ 第 3 次直接不带
    assert "extra_body" in client.completions.calls[0]
    assert "extra_body" not in client.completions.calls[1]
    assert "extra_body" not in client.completions.calls[2]
    _reset_thinking_flag()


def test_non_bad_request_errors_are_not_swallowed():
    """超时/限流不代表端点不支持这个参数，不能据此记住。

    记住了会让后续所有辅助调用永远吃不到这个优化——而超时是最常见的偶发错误。
    """
    _reset_thinking_flag()
    client = _RecordingClient(errors=[TimeoutError("slow")])
    adapter = _adapter_with(client)
    with pytest.raises(TimeoutError):
        run(
            adapter.complete(
                messages=[{"role": "user", "content": "q"}],
                tools=[],
                model="m",
                purpose="rerank",
            )
        )
    assert OpenAICompatibleAdapter._thinking_opt_out_supported is True
