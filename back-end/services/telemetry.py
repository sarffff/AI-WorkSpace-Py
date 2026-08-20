"""轻量 trace 埋点。

数据模型照搬 OpenTelemetry 的核心形状：一次回答是一棵 span 树，
``chat.turn`` 为根，下面挂每一轮模型调用、每一次工具执行、每一次检索。
每个 span 记录耗时、状态、token 用量与成本，于是「这次回答花了多少钱、
时间耗在哪一段」变成可以直接查询的事实，而不是靠猜。

三个刻意的设计约束：

1. **父子关系由 contextvars 自动传播**，调用方不用把 parent_id 一层层往下传。
   ``model_adapter`` / ``retriever`` / ``embedding_service`` 埋在三层深处，
   显式传参会污染每一个函数签名。这也是 OTel 自己的做法。
2. **没有活跃 trace 时，埋点是无副作用的空操作**。eval 脚本、单元测试、
   被直接调用的 service 都不该因为「忘了开 trace」而报错。
3. **span 只在内存累积，回答结束后一次性批量落库**，且用独立 session。
   流式热路径上不插入数据库写入；埋点失败只记日志，永远不影响业务请求。

安全约束：attributes 只放元数据（模型名、轮次、候选数、命中通道），
不放提示词或用户文本。``TELEMETRY_ATTR_MAX_CHARS`` 是防止将来误加字段的兜底。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator

from config import settings
from services import clock

logger = logging.getLogger("telemetry")


class SpanKind(str, Enum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    EMBEDDING = "embedding"


class TokenSource(str, Enum):
    """token 数是提供商回传的还是本地估算的——聚合成本时必须能区分。"""

    PROVIDER = "provider"
    ESTIMATED = "estimated"


@dataclass(slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: SpanKind
    started_at: datetime
    started_perf: float
    duration_ms: int | None = None
    status: str = "ok"
    error_type: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # prompt_tokens 里被提供商上下文缓存命中的那部分(``prompt_tokens_details
    # .cached_tokens``)。它是 prompt_tokens 的**子集**,不是额外的量——算成本时
    # 必须先减掉再按打折价单独计，否则会把缓存命中算成两份输入。
    # None 表示"这次调用没有缓存信息"(提供商没回传或本地估算),不是"命中 0 个"。
    cached_tokens: int | None = None
    token_source: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attributes: Any) -> None:
        """补充元数据。None 值直接忽略，避免在库里存一堆空字段。"""
        for key, value in attributes.items():
            if value is not None:
                self.attributes[key] = value

    def set_usage(
        self,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        source: TokenSource = TokenSource.PROVIDER,
        model: str | None = None,
        cached_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens
        self.token_source = source.value
        if model:
            self.model = model

    def fail(self, exc: BaseException) -> None:
        # 用户中断流式生成会以 GeneratorExit/CancelledError 冒出来。
        # 那不是故障，混进 error 里会污染错误率统计。
        if isinstance(exc, (GeneratorExit, asyncio.CancelledError)):
            self.status = "cancelled"
        else:
            self.status = "error"
        self.error_type = type(exc).__name__


class NoopSpan:
    """没有活跃 trace 时返回的占位对象，所有写入被丢弃。"""

    status = "ok"

    def set(self, **attributes: Any) -> None:
        return None

    def set_usage(self, **kwargs: Any) -> None:
        return None

    def fail(self, exc: BaseException) -> None:
        return None


@dataclass(slots=True)
class Trace:
    trace_id: str
    user_id: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    spans: list[Span] = field(default_factory=list)
    # 作用域为整条 trace 的默认属性，会合并进之后创建的每个 span。
    # 类似 OTel 的 baggage：调用方不必把「第几轮」一层层传进适配器。
    defaults: dict[str, Any] = field(default_factory=dict)


_current_trace: ContextVar[Trace | None] = ContextVar("_current_trace", default=None)
_current_span: ContextVar[Span | None] = ContextVar("_current_span", default=None)


def _now() -> datetime:
    # 与业务表的 func.now() 对齐，否则 span 时间和消息时间会差一个时区
    return clock.now()


def _encode_attributes(attributes: dict[str, Any]) -> str | None:
    if not attributes:
        return None
    try:
        encoded = json.dumps(attributes, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    limit = max(0, settings.TELEMETRY_ATTR_MAX_CHARS)
    if limit and len(encoded) > limit:
        return encoded[:limit]
    return encoded


class Tracer:
    @property
    def enabled(self) -> bool:
        return settings.TELEMETRY_ENABLED

    @asynccontextmanager
    async def trace(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
    ) -> AsyncIterator[Trace | None]:
        """开启一次 trace，退出时把整棵树批量落库。"""
        if not self.enabled:
            yield None
            return

        trace = Trace(
            trace_id=uuid.uuid4().hex,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        trace_token = _current_trace.set(trace)
        span_token = _current_span.set(None)
        try:
            yield trace
        finally:
            _current_span.reset(span_token)
            _current_trace.reset(trace_token)
            await self._flush(trace)

    @asynccontextmanager
    async def span(
        self, name: str, kind: SpanKind, **attributes: Any
    ) -> AsyncIterator[Span | NoopSpan]:
        trace = _current_trace.get()
        if trace is None:
            yield NoopSpan()
            return

        parent = _current_span.get()
        span = Span(
            trace_id=trace.trace_id,
            span_id=uuid.uuid4().hex,
            parent_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            started_at=_now(),
            started_perf=time.perf_counter(),
        )
        span.set(**trace.defaults)
        span.set(**attributes)
        trace.spans.append(span)
        span_token = _current_span.set(span)
        try:
            yield span
        except BaseException as exc:
            span.fail(exc)
            raise
        finally:
            span.duration_ms = int((time.perf_counter() - span.started_perf) * 1000)
            _current_span.reset(span_token)

    async def _flush(self, trace: Trace) -> None:
        if not trace.spans:
            return
        try:
            # 同步 ORM 写入放到线程里，别卡住事件循环
            await asyncio.to_thread(_persist, trace)
        except Exception as exc:
            # 埋点永远不能影响业务请求
            logger.warning("Telemetry flush failed: %s", type(exc).__name__)


def _persist(trace: Trace) -> None:
    """用独立 session 落库，使埋点的事务边界与业务请求完全解耦。"""
    from database import SessionLocal
    from models import TraceSpan
    from services.pricing import estimate_cost

    session = SessionLocal()
    try:
        rows = []
        for span in trace.spans:
            cost = estimate_cost(
                span.model,
                span.prompt_tokens,
                span.completion_tokens,
                span.cached_tokens,
            )
            rows.append(
                TraceSpan(
                    id=span.span_id,
                    trace_id=span.trace_id,
                    parent_id=span.parent_id,
                    name=span.name,
                    kind=span.kind.value,
                    user_id=trace.user_id,
                    chat_id=trace.chat_id,
                    message_id=trace.message_id,
                    started_at=span.started_at.replace(tzinfo=None),
                    duration_ms=span.duration_ms,
                    status=span.status,
                    error_type=span.error_type,
                    model=span.model,
                    prompt_tokens=span.prompt_tokens,
                    completion_tokens=span.completion_tokens,
                    cached_tokens=span.cached_tokens,
                    token_source=span.token_source,
                    cost=cost.amount if cost else None,
                    currency=cost.currency if cost else None,
                    attributes=_encode_attributes(span.attributes),
                )
            )
        session.add_all(rows)
        session.commit()
    finally:
        session.close()


tracer = Tracer()


def current_span() -> Span | NoopSpan:
    """拿到当前活跃 span，用于在不新建 span 的情况下补充属性。"""
    return _current_span.get() or NoopSpan()


def current_trace_id() -> str | None:
    """当前 trace 的 id，埋点关闭时为 None。

    ``agent_runs`` 用它把一次执行接到埋点树上：成本与耗时都在 ``trace_spans``
    那边，这里只存一个外键，不重复存一份数字——两份数字迟早会不一致。
    """
    trace = _current_trace.get()
    return trace.trace_id if trace is not None else None


def set_span_defaults(**attributes: Any) -> None:
    """给本条 trace 后续创建的所有 span 追加默认属性。

    典型用法是在 Agent 循环每轮开头写一次 ``round=n``，之后这一轮里
    嵌套多深的 span（模型调用、工具、检索、向量化）都会自动带上轮次，
    不需要把参数一路传进适配器。
    """
    trace = _current_trace.get()
    if trace is None:
        return
    trace.defaults.update(
        {key: value for key, value in attributes.items() if value is not None}
    )
