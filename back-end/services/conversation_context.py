"""对话历史的上下文工程：token 预算裁剪 + 滚动摘要。

「最近 N 条」这种窗口有两个毛病：条数和 token 不成比例，以及一旦滑出窗口，
早期约定（用户的偏好、已确认的结论、未完成的任务）就彻底消失。这里改成
按 token 预算保留最近对话，并把滑出去的部分压成一段滚动摘要接在最前面。

摘要按「已摘要消息集合的指纹」缓存，只有新消息掉出窗口时才增量重算，
避免每轮都为同一批历史付一次模型调用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from config import settings
from redis_service import redis_service
from services.token_budget import (
    HistoryMessage,
    TokenCounter,
    get_token_counter,
    plan_history,
)

logger = logging.getLogger("conversation_context")

_SUMMARY_TTL_SECONDS = 7 * 24 * 3600


@dataclass(slots=True)
class BuiltContext:
    """可直接拼进请求的历史消息，以及这次裁剪做了什么。"""

    messages: list[dict[str, str]]
    kept: int = 0
    summarized: int = 0

    @property
    def compacted(self) -> bool:
        return self.summarized > 0


class _SummaryStore:
    """摘要缓存。有 Redis 用 Redis，否则退化为进程内字典（与偏好设置同套路）。"""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(chat_id: str) -> str:
        return f"ai_workspace:summary:{chat_id}"

    def get(self, chat_id: str) -> dict[str, Any] | None:
        if redis_service.enabled and redis_service.client:
            raw = redis_service.client.get(self._key(chat_id))
            if not raw:
                return None
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
        with self._lock:
            return self._memory.get(chat_id)

    def set(self, chat_id: str, value: dict[str, Any]) -> None:
        if redis_service.enabled and redis_service.client:
            redis_service.client.set(
                self._key(chat_id),
                json.dumps(value, ensure_ascii=False),
                ex=_SUMMARY_TTL_SECONDS,
            )
            return
        with self._lock:
            self._memory[chat_id] = value


summary_store = _SummaryStore()


def _fingerprint(messages: list[HistoryMessage]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.id.encode("utf-8"))
    return digest.hexdigest()


_SUMMARY_INSTRUCTION = (
    "把下面的对话压缩成要点摘要，供后续对话继续使用。必须保留：用户的目标与偏好、"
    "已经确认的事实与结论、尚未完成的任务、明确的约束。省略寒暄与重复表述。"
    "直接输出摘要正文，不要加标题或解释。"
)


class ConversationContextBuilder:
    """把完整历史压成预算内的消息列表。"""

    def __init__(self, model_adapter, counter: TokenCounter | None = None) -> None:
        self._model_adapter = model_adapter
        self._counter = counter or get_token_counter(settings.TOKEN_COUNTER)

    async def build(self, chat_id: str, history: list[HistoryMessage]) -> BuiltContext:
        plan = plan_history(
            history,
            counter=self._counter,
            budget_tokens=settings.HISTORY_TOKEN_BUDGET,
        )
        messages = [message.as_api_message() for message in plan.kept]

        if not plan.dropped:
            return BuiltContext(messages=messages, kept=len(plan.kept))
        if not settings.HISTORY_SUMMARY:
            # 关掉摘要就是纯滑窗：早期上下文直接丢弃
            return BuiltContext(messages=messages, kept=len(plan.kept))

        summary = await self._rolling_summary(chat_id, plan.dropped)
        if not summary:
            # 摘要没做出来（模型故障等）就退化成纯滑窗，不谎报"已压缩"
            return BuiltContext(messages=messages, kept=len(plan.kept))
        messages.insert(0, {"role": "system", "content": f"[更早对话的摘要]\n{summary}"})
        return BuiltContext(
            messages=messages, kept=len(plan.kept), summarized=len(plan.dropped)
        )

    async def _rolling_summary(
        self, chat_id: str, dropped: list[HistoryMessage]
    ) -> str:
        fingerprint = _fingerprint(dropped)
        cached = summary_store.get(chat_id) or {}
        if cached.get("fingerprint") == fingerprint:
            return str(cached.get("summary") or "")

        previous = str(cached.get("summary") or "")
        already = int(cached.get("count") or 0)
        if already > len(dropped):
            # 历史被截断过（编辑/重新生成会删掉后续分支），旧摘要不再可信
            previous, already = "", 0

        pending = dropped[already:]
        if not pending:
            return previous

        summary = await self._summarize(previous, pending)
        if not summary:
            return previous

        summary_store.set(
            chat_id,
            {"fingerprint": fingerprint, "summary": summary, "count": len(dropped)},
        )
        return summary

    async def _summarize(
        self, previous: str, messages: list[HistoryMessage]
    ) -> str:
        transcript = "\n".join(
            f"{message.role}: {message.content}" for message in messages
        )
        sections = [_SUMMARY_INSTRUCTION]
        if previous:
            sections.append(f"[已有摘要]\n{previous}")
        sections.append(f"[新增对话]\n{transcript}")

        try:
            completion = await self._model_adapter.complete(
                messages=[{"role": "user", "content": "\n\n".join(sections)}],
                tools=[],
                model=settings.utility_model,
                temperature=0.2,
                max_tokens=settings.HISTORY_SUMMARY_MAX_TOKENS,
                purpose="summary",
            )
        except Exception as exc:
            logger.warning("history summarization failed: %s", type(exc).__name__)
            return ""

        content = (completion.content or "").strip()
        return content
