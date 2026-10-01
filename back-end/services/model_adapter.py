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
from services.token_budget import count_message_tokens, get_token_counter, message_text

logger = logging.getLogger("model_adapter")


def _is_bad_request(exc: Exception) -> bool:
    """粗判「参数不被接受」，用于探测提供商是否支持某个可选字段。"""
    return (
        getattr(exc, "status_code", None) == 400
        or type(exc).__name__ == "BadRequestError"
    )


def _failover_worthy(exc: Exception) -> bool:
    """这个异常值不值得换备用模型再试一次。

    只对「换个模型可能就好了」的错降级：限流(429)、服务端故障(5xx)、超时与
    连接失败、以及模型名不存在(404——换一个存在的名就对了)。SDK 的连接/超时
    类异常不一定带 status_code，所以先按类名认。

    400 / 401 / 403 不降级：那是请求本身的错(参数/鉴权)，同一个 endpoint 上换
    个模型修不好，再试一次只是白白多花一次往返延迟，还会把真正的错因掩在
    一堆重试后面。认不出的异常保守起见也不降级(当请求错处理)，避免在一个本
    就会失败的请求上把备用模型的额度也烧掉。
    """
    name = type(exc).__name__
    if name in (
        "APIConnectionError",
        "APITimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
    ):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status == 404 or status >= 500
    return False


@dataclass(frozen=True, slots=True)
class _Candidate:
    """故障切换链上的一个候选：用哪个 client、哪个模型、给日志/telemetry 的标签。"""

    client: Any
    model: str
    label: str  # "primary"（主 endpoint）或备用提供商的 host


def _estimate_usage(
    messages: list[dict[str, Any]], output_text: str
) -> tuple[int, int]:
    """提供商没回传 usage 时的本地估算。

    只算消息正文，不含工具 schema 与提供商内部的特殊 token，所以会低估。
    多模态消息的 ``content`` 是内容块列表，这里只取其中的文本——图像按面积
    折算 token，没法按字符估。落库时会标成 ``estimated``，聚合成本时必须与
    真实用量区分开。
    """
    counter = get_token_counter(settings.TOKEN_COUNTER)
    prompt = sum(
        count_message_tokens(
            {
                "role": str(message.get("role") or ""),
                "content": message_text(message.get("content")),
            },
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
    # 提供商回传的终止原因(stop / length / tool_calls / ...)。
    #
    # 为什么必须带出来:混合推理模型会先花 max_tokens 思考,预算不够时返回的
    # content 是**空串**,而不是截断到一半的正文。没有这个字段,"模型没什么要说"
    # 和"预算被思考吃光了"在调用方看来完全同形——两者都是 content 为空。
    # 2026-08-22 实测全库 7 个辅助调用点有 5 个栽在这上面(见
    # scripts/probe_structured_budgets.py),其中记忆抽取 100% 失效了很久。
    #
    # None 表示提供商没给(老端点或非标准实现),不等于 "stop"。
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """输出是否因为撞到 max_tokens 而中止。"""
        return self.finish_reason == "length"

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

        # 超时与重试在这里就定死,而不是留给 SDK 默认(600s × 3 = 最坏 1800 秒)。
        # 辅助调用会在 with_options 里再收紧一次,见 _create_completion。
        self._client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            timeout=settings.LLM_CHAT_TIMEOUT_SECONDS,
            max_retries=settings.LLM_CHAT_MAX_RETRIES,
        )
        # 跨提供商兜底客户端，惰性建一次并缓存（见 _fallback_provider_clients）
        self._provider_clients: list[_Candidate] | None = None

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
            aux = purpose != "chat"
            if aux:
                # 辅助调用（重排 / HyDE / 改写 / 摘要）全都有降级路径，为一个可放弃的
                # 增强等 SDK 默认的 600s×3 是纯亏，所以单独收紧超时与重试。with_options
                # 是浅拷贝、共用连接池，不会每次调用都丢 keep-alive；按候选逐个套用。
                span.set(auxiliary_timeout=settings.LLM_AUXILIARY_TIMEOUT_SECONDS)
            request = self._build_request(
                messages, tools, model, temperature, max_tokens, top_p
            )
            # 与流式同一套故障切换：按候选链试（主端点模型链 + 跨提供商），只对可切换
            # 的错降级。非流式没有"字节已给用户"的顾虑，但保持同一套链与判据更好推。
            chain = self._candidate_chain(model)
            response = None
            used_model = model
            for idx, cand in enumerate(chain):
                eff_client = (
                    cand.client.with_options(
                        timeout=settings.LLM_AUXILIARY_TIMEOUT_SECONDS,
                        max_retries=settings.LLM_AUXILIARY_MAX_RETRIES,
                    )
                    if aux
                    else cand.client
                )
                attempt = {**request, "model": cand.model}
                try:
                    response = await self._create_completion(
                        eff_client, attempt, auxiliary=aux, span=span
                    )
                    used_model = cand.model
                    if idx > 0:
                        span.set(fallback_model=cand.model, fallback_attempts=idx)
                        if cand.label != "primary":
                            span.set(fallback_provider=cand.label)
                        logger.warning(
                            "llm.%s: using fallback %s@%s (after %s failure(s))",
                            purpose,
                            cand.model,
                            cand.label,
                            idx,
                        )
                    break
                except Exception as exc:
                    if idx == len(chain) - 1 or not _failover_worthy(exc):
                        raise
                    logger.warning(
                        "llm.%s: candidate %s@%s failed (%s), falling back",
                        purpose,
                        cand.model,
                        cand.label,
                        type(exc).__name__,
                    )
                    continue
            if not response.choices:
                span.set(empty_response=True)
                return ModelCompletion(
                    content="", tool_calls=[], protocol_error="模型未返回候选结果"
                )

            message = response.choices[0].message
            content = message.content if isinstance(message.content, str) else ""
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            standard_calls = [
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in (message.tool_calls or [])
            ]
            self._record_usage(
                span, getattr(response, "usage", None), messages, content, used_model
            )
            span.set(tool_calls=len(standard_calls) or None)
            self._record_truncation(span, finish_reason, content, max_tokens, purpose)
            return self._build_completion(
                content, standard_calls, finish_reason=finish_reason
            )

    @staticmethod
    def _record_truncation(
        span: Any,
        finish_reason: str | None,
        content: str,
        max_tokens: int,
        purpose: str,
    ) -> None:
        """撞到 max_tokens 时留下痕迹——埋点一条,空输出再加一条日志。

        分两级是有意的:``finish_reason=length`` 而正文非空是常见的"答长了",
        属于成本项,记进 span 供事后统计就够;而 **正文为空** 的截断是故障,
        意味着预算全被思考吃掉、调用方拿到的是空串,它会走静默降级路径,
        所以必须当场喊出来。这正是记忆抽取失效很久没人发现的原因。
        """
        if finish_reason != "length":
            return
        span.set(truncated=True)
        if content.strip():
            return
        logger.warning(
            "llm.%s returned empty content and finish_reason=length: "
            "max_tokens=%s was fully consumed (reasoning models spend it on thinking "
            "before emitting any text) — raise the budget for this call site",
            purpose,
            max_tokens,
        )

    @staticmethod
    def _cached_tokens(usage: Any) -> int | None:
        """读提供商回传的上下文缓存命中量。

        字段是 ``usage.prompt_tokens_details.cached_tokens``(OpenAI 与智谱都用
        这个形状)。三层都要防:整个 ``prompt_tokens_details`` 可能不存在(老端点)、
        存在但为 None、或者是个 dict 而不是对象(某些兼容实现不做模型化)。

        取不到时返回 None 而不是 0——"没有缓存信息"和"命中 0 个"是两件事,
        前者不该被面板算进命中率的分母。
        """
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        if details is None:
            return None
        cached = (
            details.get("cached_tokens")
            if isinstance(details, dict)
            else getattr(details, "cached_tokens", None)
        )
        if not isinstance(cached, int) or isinstance(cached, bool) or cached < 0:
            return None
        return cached

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
            # 估算路径下不报缓存命中:本地估算根本不知道提供商那边命中了什么,
            # 填 0 会被面板读成"这次一点没命中"。
            cached_tokens = None
        else:
            source = TokenSource.PROVIDER
            cached_tokens = OpenAICompatibleAdapter._cached_tokens(usage)
        span.set_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            source=source,
            model=model,
            cached_tokens=cached_tokens,
        )
        # 命中率进 attributes 而不是只留原始数:排查"缓存到底有没有生效"时看的是
        # 比例,而 prompt_tokens 每轮都在变,两个绝对数摆在一起看不出趋势。
        if cached_tokens is not None and prompt_tokens:
            span.set(cache_hit_ratio=round(cached_tokens / prompt_tokens, 4))

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
        finish_reason: str | None = None,
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
                finish_reason=finish_reason,
            )

        text_calls = cls.parse_text_tool_calls(content)
        if text_calls:
            return ModelCompletion(
                content="",
                tool_calls=text_calls,
                raw_content=content,
                uses_text_tool_protocol=True,
                finish_reason=finish_reason,
            )

        if "<function=call>" in content.lower():
            return ModelCompletion(
                content="",
                tool_calls=[],
                protocol_error="模型返回了无法解析的工具调用格式",
                finish_reason=finish_reason,
            )

        return ModelCompletion(
            content=content,
            tool_calls=[],
            streamed_length=streamed_length,
            finish_reason=finish_reason,
        )

    # 部分 OpenAI 兼容端点不认 stream_options。被拒一次就记住，不再重复试探。
    _stream_usage_supported = True
    # 同一个套路:辅助调用尝试关掉思考链,端点不认就记住并不再试探。
    #
    # 为什么值得关:实测 20 候选的 listwise 重排,开思考 20.3 秒 / 1011 输出 token,
    # 关掉 0.4 秒 / 7 token——**快 50 倍**,而且排序结果更好(开着时那 1011 token
    # 想了半天只吐出 [10],关掉给出 [10, 20])。排序不需要思考链,而思考链的代价
    # 恰恰是这个调用点唯一的瓶颈。
    #
    # 只对辅助调用做。主回答那次的思考链是有价值的,不能顺手关掉。
    _thinking_opt_out_supported = True

    async def _create_completion(
        self, client: Any, request: dict[str, Any], *, auxiliary: bool, span: Any
    ) -> Any:
        """发一次非流式请求。辅助调用先试着关掉思考链。

        被端点拒绝时**只回退一次并记住**,而不是每次调用都白试一遍——和
        ``_stream_usage_supported`` 同一个套路。只认 400 类错误(``_is_bad_request``):
        超时或限流不代表端点不支持这个参数,那种情况下记住会让后续调用永远吃不到
        这个优化。
        """
        if auxiliary and self._thinking_opt_out_supported:
            try:
                result = await client.chat.completions.create(
                    **request, extra_body={"thinking": {"type": "disabled"}}
                )
                span.set(thinking_disabled=True)
                return result
            except Exception as exc:
                if not _is_bad_request(exc):
                    raise
                type(self)._thinking_opt_out_supported = False
                logger.info(
                    "Provider rejected thinking opt-out; auxiliary calls will "
                    "keep the reasoning chain (slower)."
                )
        return await client.chat.completions.create(**request)

    async def _open_stream(self, request: dict[str, Any], client: Any = None) -> Any:
        """开流。尽量带上 include_usage，被提供商拒绝则降级为本地估算。

        超时与重试来自构造函数里配的客户端(``LLM_CHAT_*``)。对流式来说这两件事
        的语义值得写清楚,因为它们和非流式不一样:

        - **超时**是「两个分片之间最多等多久」,每次读都重置。所以它不会切断一个
          正常输出的长回答,只会掐掉「连上了但不再吐字节」的挂死连接。
        - **重试**只发生在这个函数里,也就是**开流之前**。一旦返回了 stream 对象、
          调用方开始迭代,失败就直接抛给调用方,不会重放已经发给用户的内容。
          这是「主回答可以重试 2 次」在流式下依然安全的全部理由。
        """
        active = client or self._client
        if self._stream_usage_supported and settings.LLM_STREAM_USAGE:
            try:
                return await active.chat.completions.create(
                    **request, stream_options={"include_usage": True}
                )
            except Exception as exc:
                if not _is_bad_request(exc):
                    raise
                type(self)._stream_usage_supported = False
                logger.info(
                    "Provider rejected stream_options; token usage will be estimated."
                )
        return await active.chat.completions.create(**request)

    @staticmethod
    def _model_chain(model: str) -> list[str]:
        """主模型 + 配置的备用模型，去重、去空、去掉与主模型重名的。

        默认没配 ``LLM_FALLBACK_MODELS`` 时返回 ``[model]`` 单元素——于是下面
        那两处循环各只跑一次、异常直接抛，与加这个功能之前逐位相同。
        """
        chain = [model]
        raw = (settings.LLM_FALLBACK_MODELS or "").strip()
        if raw:
            for name in raw.split(","):
                name = name.strip()
                if name and name not in chain:
                    chain.append(name)
        return chain

    def _fallback_provider_clients(self) -> list[_Candidate]:
        """解析 LLM_FALLBACK_PROVIDERS，建好跨提供商候选（client + model），缓存。

        客户端只建一次（连接池可复用）。JSON 坏了、或某项不合法，只告警并跳过——
        配错不该让主回答跟着挂，退回"没有跨提供商兜底"即可（同项目一贯的 fail-safe）。
        """
        # getattr 而非直接取属性：有测试用 __new__ 造实例、不走 __init__，
        # 那时这个属性还不存在
        cached = getattr(self, "_provider_clients", None)
        if cached is not None:
            return cached
        from openai import AsyncOpenAI
        from urllib.parse import urlparse

        candidates: list[_Candidate] = []
        raw = (settings.LLM_FALLBACK_PROVIDERS or "").strip()
        if raw:
            try:
                entries = json.loads(raw)
                if not isinstance(entries, list):
                    raise ValueError("应是 JSON 数组")
            except Exception as exc:
                logger.warning(
                    "LLM_FALLBACK_PROVIDERS 解析失败，忽略跨提供商兜底: %s",
                    type(exc).__name__,
                )
                entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                model = str(entry.get("model") or "").strip()
                if not model:
                    logger.warning("LLM_FALLBACK_PROVIDERS 某项缺 model，跳过")
                    continue
                base_url = str(entry.get("base_url") or settings.LLM_BASE_URL)
                client = AsyncOpenAI(
                    api_key=str(entry.get("api_key") or settings.LLM_API_KEY),
                    base_url=base_url,
                    timeout=settings.LLM_CHAT_TIMEOUT_SECONDS,
                    max_retries=settings.LLM_CHAT_MAX_RETRIES,
                )
                label = (urlparse(base_url).hostname or base_url)[:60]
                candidates.append(_Candidate(client, model, label))
        self._provider_clients = candidates
        return candidates

    def _candidate_chain(self, model: str) -> list[_Candidate]:
        """完整故障切换链：主 endpoint 的模型链 + 跨提供商候选。

        默认两者都没配时就是 ``[(主 client, model, "primary")]`` 单元素——下面两处
        循环各只跑一次、异常直接抛，与加这些功能之前逐位相同。
        """
        chain = [
            _Candidate(self._client, name, "primary")
            for name in self._model_chain(model)
        ]
        chain.extend(self._fallback_provider_clients())
        return chain

    async def _open_stream_with_failover(
        self, request: dict[str, Any], span: Any, purpose: str
    ) -> tuple[Any, str]:
        """按候选链开流（主端点模型链 + 跨提供商），返回（stream, 实际用的模型名）。

        只在**开流之前**切换：``_open_stream`` 里那次 ``create`` 报错时尚未有任何
        字节流给用户，换候选是安全的；一旦返回了 stream 对象、调用方开始迭代，
        失败就直接抛（同 ``_open_stream`` 的文档串）。默认单候选链时这与改动前一致。
        """
        chain = self._candidate_chain(request["model"])
        for idx, cand in enumerate(chain):
            attempt = {**request, "model": cand.model}
            try:
                stream = await self._open_stream(attempt, cand.client)
            except Exception as exc:
                # 最后一个候选也失败、或这个错不值得换 → 抛，交给上层。
                if idx == len(chain) - 1 or not _failover_worthy(exc):
                    raise
                logger.warning(
                    "llm.%s: candidate %s@%s failed to open stream (%s), falling back",
                    purpose,
                    cand.model,
                    cand.label,
                    type(exc).__name__,
                )
                continue
            if idx > 0:
                span.set(fallback_model=cand.model, fallback_attempts=idx)
                if cand.label != "primary":
                    span.set(fallback_provider=cand.label)
                logger.warning(
                    "llm.%s: using fallback %s@%s (after %s failure(s))",
                    purpose,
                    cand.model,
                    cand.label,
                    idx,
                )
            return stream, cand.model
        # 到不了这里：循环要么 return 要么 raise。留着只为类型检查器。
        raise RuntimeError("candidate chain exhausted")

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
            stream, used_model = await self._open_stream_with_failover(
                self._build_request(
                    messages, tools, model, temperature, max_tokens, top_p, stream=True
                ),
                span,
                purpose,
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
            finish_reason: str | None = None

            async for chunk in stream:
                # include_usage 生效时,用量在最后一个不含 choices 的分片里到达
                usage = getattr(chunk, "usage", None) or usage
                if not chunk.choices:
                    continue
                # 终止原因只出现在最后一个带 choices 的分片上,前面的分片是 None
                finish_reason = (
                    getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                )
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
            self._record_usage(span, usage, messages, content, used_model)
            # 首 token 延迟是流式体验的关键指标,和总耗时分开记
            span.set(
                first_token_ms=first_token_ms,
                tool_calls=len(standard_calls) or None,
            )
            self._record_truncation(span, finish_reason, content, max_tokens, purpose)
            yield StreamChunk(
                completion=self._build_completion(
                    content, standard_calls, streamed_length, finish_reason
                )
            )
