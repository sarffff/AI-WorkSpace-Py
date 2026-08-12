from __future__ import annotations

import html
import json
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from config import settings
from services.telemetry import SpanKind, TokenSource, tracer
from services.token_budget import count_message_tokens, get_token_counter

logger = logging.getLogger("model_adapter")


def _is_bad_request(exc: Exception) -> bool:
    """粗判「参数不被接受」，用于探测提供商是否支持某个可选字段。"""
    return (
        getattr(exc, "status_code", None) == 400
        or type(exc).__name__ == "BadRequestError"
    )


def _estimate_usage(
    messages: list[dict[str, Any]], output_text: str
) -> tuple[int, int]:
    """提供商没回传 usage 时的本地估算。

    只算消息正文，不含工具 schema 与提供商内部的特殊 token，所以会低估。
    落库时会标成 ``estimated``，聚合成本时必须与真实用量区分开。
    """
    counter = get_token_counter(settings.TOKEN_COUNTER)
    prompt = sum(
        count_message_tokens(
            {"role": str(message.get("role") or ""), "content": message.get("content") or ""},
            counter,
        )
        for message in messages
    )
    return prompt, counter.count(output_text)
@dataclass(slots=True)
class ToolCall:
    """提供商中立的单个应用工具调用请求。"""

    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ModelCompletion:
    """一次完整的模型回复,在调用任何请求的工具之前。"""

    content: str
    tool_calls: list[ToolCall]
    raw_content: str | None = None
    uses_text_tool_protocol: bool = False
    protocol_error: str | None = None
    # content 中已在流式阶段透出给用户的前缀长度,调用方据此避免重复输出。
    streamed_length: int = 0

    def as_assistant_message(self) -> dict[str, Any]:
        """返回该轮次对应的正确继续消息。"""
        if self.uses_text_tool_protocol:
            return {"role": "assistant", "content": self.raw_content or ""}

        if not self.tool_calls:
            return {"role": "assistant", "content": self.content}

        return {
            "role": "assistant",
            "content": self.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ],
        }


@dataclass(slots=True)
class StreamChunk:
    """流式过程中的单个事件。

    ``text`` 为可安全透出给用户的文本增量;``completion`` 仅在本轮结束时出现一次,
    携带装配完毕的完整结果(含工具调用)。两者互斥。
    """

    text: str | None = None
    completion: ModelCompletion | None = None


class ModelAdapter(ABC):
    """所有模型/提供商适配器必须实现的接口契约。"""

    @abstractmethod
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        purpose: str = "chat",
    ) -> ModelCompletion:
        """``purpose`` 只用于埋点分类(chat / summary / query_rewrite / rerank)。

        让适配器自己知道用途，才能保证 token 用量一定被记下来，
        而不依赖每个调用方都记得手工开 span。
        """
        raise NotImplementedError

    @abstractmethod
    async def stream_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        purpose: str = "chat",
    ) -> AsyncGenerator[StreamChunk, None]:
        """流式执行一轮模型调用。

        实现必须:
        1. 边到边地 yield 可安全展示的文本增量(不得包含提供商内部工具标记);
        2. 在流结束时 yield 恰好一个携带 ``completion`` 的事件。
        """
        raise NotImplementedError


class OpenAICompatibleAdapter(ModelAdapter):
    """适配器:OpenAI 兼容的 chat-completions 端点。

    同时处理标准的 ``tool_calls`` JSON 字段以及某些兼容提供商输出的 XML 风格
    ``<function=call>`` 格式标记。后者在本适配器内完全解析,
    不会作为用户内容透传给业务层。
    """

    _FUNCTION_CALL_RE = re.compile(
        r'<function\s*=\s*call>\s*'
        r'<invoke\s+name\s*=\s*["\'](?P<name>[^"\']+)["\']\s*>'
        r'(?P<parameters>.*?)</invoke>\s*</function>',
        re.IGNORECASE | re.DOTALL,
    )
    _PARAMETER_RE = re.compile(
        r'<parameter\s+name\s*=\s*["\'](?P<name>[^"\']+)["\']\s*>'
        r'(?P<value>.*?)</parameter>',
        re.IGNORECASE | re.DOTALL,
    )
    # 文本工具协议的起始标记。流式阶段用它判断一段内容能否安全透出给用户。
    _TEXT_PROTOCOL_MARKER = "<function"

    def __init__(self) -> None:
        from openai import AsyncOpenAI
        from config import settings

        self._client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    @classmethod
    def _may_be_text_tool_call(cls, buffer: str) -> bool:
        """缓冲区是否仍有可能是文本工具协议的开头。

        为 True 时调用方必须继续缓冲而不能透出:要么已确认出现标记,
        要么当前内容还只是标记的一个前缀(如 ``"<fun"``),尚无法判定。
        """
        stripped = buffer.lstrip()
        if not stripped:
            return True
        lowered = stripped.lower()
        if cls._TEXT_PROTOCOL_MARKER in lowered:
            return True
        return cls._TEXT_PROTOCOL_MARKER.startswith(
            lowered[: len(cls._TEXT_PROTOCOL_MARKER)]
        )

    @classmethod
    def parse_text_tool_calls(cls, content: str) -> list[ToolCall] | None:
        """解析完整的文本函数调用轮次(如果是的话)。

        对于部分或混合响应,有意不视为工具调用;
        调用方可将其作为协议错误暴露,而非执行仅形似函数调用的文本。
        """
        matches = list(cls._FUNCTION_CALL_RE.finditer(content))
        if not matches:
            return None

        remaining = cls._FUNCTION_CALL_RE.sub("", content).strip()
        if remaining:
            return None

        calls: list[ToolCall] = []
        for match in matches:
            arguments = {
                parameter.group("name"): html.unescape(parameter.group("value").strip())
                for parameter in cls._PARAMETER_RE.finditer(match.group("parameters"))
            }
            calls.append(
                ToolCall(
                    id=f"text-call-{uuid.uuid4().hex}",
                    name=match.group("name"),
                    arguments=json.dumps(arguments, ensure_ascii=False),
                )
            )
        return calls

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        purpose: str = "chat",
    ) -> ModelCompletion:
        """完成一轮模型回复,同时处理标准和文本工具协议。

    模型可通过以下方式返回工具调用:
    1. 标准 OpenAI JSON:message.tool_calls 数组(优先)
    2. 文本函数标记:<function=call><invoke name="...">...
       由智谱/GLM 等模型的 OpenAI 兼容端点输出。
    当两者都存在时优先使用 JSON;都不存在则回落到文本生成,
    以保证流式器仍能展示最终内容。
    """
        async with tracer.span(
            f"llm.{purpose}",
            SpanKind.LLM,
            model=model,
            streaming=False,
            tools=len(tools) or None,
        ) as span:
            response = await self._client.chat.completions.create(
                **self._build_request(
                    messages, tools, model, temperature, max_tokens, top_p
                )
            )
            if not response.choices:
                span.set(empty_response=True)
                return ModelCompletion(
                    content="", tool_calls=[], protocol_error="模型未返回候选结果"
                )

            message = response.choices[0].message
            content = message.content if isinstance(message.content, str) else ""
            standard_calls = [
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in (message.tool_calls or [])
            ]
            self._record_usage(
                span, getattr(response, "usage", None), messages, content, model
            )
            span.set(tool_calls=len(standard_calls) or None)
            return self._build_completion(content, standard_calls)

    @staticmethod
    def _record_usage(
        span: Any,
        usage: Any,
        messages: list[dict[str, Any]],
        output_text: str,
        model: str,
    ) -> None:
        """优先记录提供商回传的用量，缺失时用本地估算并标注来源。"""
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        if prompt_tokens is None and completion_tokens is None:
            prompt_tokens, completion_tokens = _estimate_usage(messages, output_text)
            source = TokenSource.ESTIMATED
        else:
            source = TokenSource.PROVIDER
        span.set_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            source=source,
            model=model,
        )

    @staticmethod
    def _build_request(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        top_p: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        if stream:
            request["stream"] = True
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        return request

    @classmethod
    def _build_completion(
        cls,
        content: str,
        standard_calls: list[ToolCall],
        streamed_length: int = 0,
    ) -> ModelCompletion:
        """把原始内容与已装配的工具调用归一化为 ModelCompletion。

        标准 JSON 工具调用优先;其次尝试解析文本函数标记;若出现标记但无法
        完整解析,则作为协议错误上报,而不是把形似函数调用的文本当正文输出。
        """
        if standard_calls:
            return ModelCompletion(
                content=content,
                tool_calls=standard_calls,
                streamed_length=streamed_length,
            )

        text_calls = cls.parse_text_tool_calls(content)
        if text_calls:
            return ModelCompletion(
                content="",
                tool_calls=text_calls,
                raw_content=content,
                uses_text_tool_protocol=True,
            )

        if "<function=call>" in content.lower():
            return ModelCompletion(
                content="",
                tool_calls=[],
                protocol_error="模型返回了无法解析的工具调用格式",
            )

        return ModelCompletion(
            content=content, tool_calls=[], streamed_length=streamed_length
        )

    # 部分 OpenAI 兼容端点不认 stream_options。被拒一次就记住，不再重复试探。
    _stream_usage_supported = True

    async def _open_stream(self, request: dict[str, Any]) -> Any:
        """开流。尽量带上 include_usage，被提供商拒绝则降级为本地估算。"""
        if self._stream_usage_supported and settings.LLM_STREAM_USAGE:
            try:
                return await self._client.chat.completions.create(
                    **request, stream_options={"include_usage": True}
                )
            except Exception as exc:
                if not _is_bad_request(exc):
                    raise
                type(self)._stream_usage_supported = False
                logger.info(
                    "Provider rejected stream_options; token usage will be estimated."
                )
        return await self._client.chat.completions.create(**request)

    async def stream_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        purpose: str = "chat",
    ) -> AsyncGenerator[StreamChunk, None]:
        """流式执行一轮调用,同时装配 tool_call 增量。

        提供商把一次工具调用拆成多个 delta 下发(``id``/``name`` 只出现在首片,
        ``arguments`` 逐片拼接),这里按 ``index`` 归并。文本增量则在确认不是
        文本工具协议开头之后才透出,因此用户永远看不到内部函数标记。
        """
        async with tracer.span(
            f"llm.{purpose}",
            SpanKind.LLM,
            model=model,
            streaming=True,
            tools=len(tools) or None,
        ) as span:
            stream = await self._open_stream(
                self._build_request(
                    messages, tools, model, temperature, max_tokens, top_p, stream=True
                )
            )

            content_parts: list[str] = []
            partial_calls: dict[int, dict[str, str]] = {}
            buffer = ""
            streamed_length = 0
            emitting = False
            # 已确认为文本工具协议或图文混合响应,后续文本一律不再透出。
            blocked = False
            usage = None
            first_token_ms: int | None = None

            async for chunk in stream:
                # include_usage 生效时,用量在最后一个不含 choices 的分片里到达
                usage = getattr(chunk, "usage", None) or usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                for raw_call in getattr(delta, "tool_calls", None) or []:
                    index = getattr(raw_call, "index", None)
                    if index is None:
                        index = len(partial_calls)
                    slot = partial_calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if raw_call.id:
                        slot["id"] = raw_call.id
                    function = getattr(raw_call, "function", None)
                    if function is not None:
                        if function.name:
                            slot["name"] = function.name
                        if function.arguments:
                            slot["arguments"] += function.arguments

                piece = delta.content
                if not piece:
                    continue
                if first_token_ms is None:
                    first_token_ms = int(
                        (time.perf_counter() - getattr(span, "started_perf", time.perf_counter()))
                        * 1000
                    )
                content_parts.append(piece)

                if blocked:
                    continue
                if emitting:
                    if self._TEXT_PROTOCOL_MARKER in piece.lower():
                        blocked = True
                        continue
                    streamed_length += len(piece)
                    yield StreamChunk(text=piece)
                    continue

                buffer += piece
                if self._may_be_text_tool_call(buffer):
                    continue
                emitting = True
                streamed_length = len(buffer)
                yield StreamChunk(text=buffer)
                buffer = ""

            standard_calls = [
                ToolCall(
                    id=slot["id"] or f"stream-call-{uuid.uuid4().hex}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
                for _index, slot in sorted(partial_calls.items())
                if slot["name"]
            ]
            content = "".join(content_parts)
            self._record_usage(span, usage, messages, content, model)
            # 首 token 延迟是流式体验的关键指标,和总耗时分开记
            span.set(
                first_token_ms=first_token_ms,
                tool_calls=len(standard_calls) or None,
            )
            yield StreamChunk(
                completion=self._build_completion(
                    content, standard_calls, streamed_length
                )
            )
