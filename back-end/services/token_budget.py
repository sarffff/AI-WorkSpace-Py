"""Token 计数与对话历史预算。

原来的历史窗口按「最近 20 条」截断，这在长短消息混排时完全失控：20 条短消息
可能只占几百 token，20 条长消息能直接撑爆上下文窗口。这里改为按 token 预算裁剪，
并把 tokenizer 做成可替换的——默认零依赖的启发式估算，需要精确时切到 tiktoken。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from math import ceil
from typing import Protocol, runtime_checkable

logger = logging.getLogger("token_budget")

# 中日韩统一表意文字 + 假名 + 谚文。这些字符基本是 1 字 1 token。
_CJK_RE = re.compile(
    r"[㐀-䶿一-鿿぀-ヿ가-힯豈-﫿]"
)


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """零依赖估算器。

    CJK 字符按 1 token 计，其余字符按约 4 字符 1 token —— 这是主流 BPE 词表在
    中英文上的经验比例。估算会有偏差，所以预算里始终留出安全余量；需要精确
    计数时换成 ``TiktokenCounter``。
    """

    LATIN_CHARS_PER_TOKEN = 4

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK_RE.findall(text))
        rest = len(text) - cjk
        return cjk + ceil(max(0, rest) / self.LATIN_CHARS_PER_TOKEN)


class TiktokenCounter:
    """精确计数器，需要额外安装 tiktoken（首次使用会下载词表文件）。"""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken

        self._encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text)) if text else 0


_counter_cache: dict[str, TokenCounter] = {}


def get_token_counter(kind: str = "heuristic") -> TokenCounter:
    """按配置返回计数器；tiktoken 不可用时回退到启发式估算。"""
    cached = _counter_cache.get(kind)
    if cached is not None:
        return cached

    counter: TokenCounter
    if kind == "tiktoken":
        try:
            counter = TiktokenCounter()
        except Exception as exc:
            logger.warning(
                "tiktoken unavailable (%s), falling back to heuristic counter",
                type(exc).__name__,
            )
            counter = HeuristicTokenCounter()
    else:
        counter = HeuristicTokenCounter()

    _counter_cache[kind] = counter
    return counter


# 每条消息除正文外还要计入 role、分隔符等固定开销，按主流实现取 4。
MESSAGE_OVERHEAD_TOKENS = 4


def count_message_tokens(message: dict[str, str], counter: TokenCounter) -> int:
    return (
        counter.count(message.get("content") or "")
        + counter.count(message.get("role") or "")
        + MESSAGE_OVERHEAD_TOKENS
    )


@dataclass(slots=True)
class HistoryMessage:
    """历史消息。带 id 是为了给摘要缓存算指纹。"""

    id: str
    role: str
    content: str

    def as_api_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class HistoryPlan:
    """历史裁剪结果。``dropped`` 是需要被摘要压缩的更早消息。"""

    kept: list[HistoryMessage] = field(default_factory=list)
    dropped: list[HistoryMessage] = field(default_factory=list)
    kept_tokens: int = 0

    @property
    def overflowed(self) -> bool:
        return bool(self.dropped)


def plan_history(
    messages: list[HistoryMessage],
    *,
    counter: TokenCounter,
    budget_tokens: int,
) -> HistoryPlan:
    """从最新往回保留能塞进预算的消息，更早的进 ``dropped``。

    单条消息永不切断：宁可整条丢给摘要，也不产生半句话的上下文。
    """
    plan = HistoryPlan()
    if budget_tokens <= 0:
        plan.dropped = list(messages)
        return plan

    kept_reversed: list[HistoryMessage] = []
    used = 0
    cutoff = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        cost = count_message_tokens(message.as_api_message(), counter)
        if used + cost > budget_tokens:
            cutoff = index + 1
            break
        used += cost
        kept_reversed.append(message)
        cutoff = index
    else:
        cutoff = 0

    kept_reversed.reverse()
    plan.kept = kept_reversed
    plan.dropped = messages[:cutoff]
    plan.kept_tokens = used
    return plan
