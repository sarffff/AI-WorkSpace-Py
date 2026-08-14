"""语义缓存。

省钱的手段里最直接的一个:同一个问题问第二遍,不必再调一次模型。难点不在缓存本身,
而在"什么时候**不能**命中"——这块的设计比实现重要得多。

## 为什么默认关闭

余弦相似度 0.95 的两个问题,答案可能完全不同:

    "2024 年的报销上限是多少"   vs   "2025 年的报销上限是多少"
    "试用期能提前转正吗"        vs   "试用期不能提前转正吗"

嵌入向量对时间、否定、数量级这些**改变答案**的差异不敏感。所以语义缓存是一个
拿正确性换成本的取舍,必须显式开启(``SEMANTIC_CACHE_ENABLED``),阈值也定得很高。
真要上生产,阈值该由评估集扫出来,而不是拍一个 0.95。

## 三条硬约束

1. **按用户隔离。** 缓存键里带 user_id。跨用户命中就是数据泄露,不是优化。
2. **知识库一变就失效。** RAG 回答依赖当时的索引;文档增删后旧答案可能已经错了,
   所以 ``invalidate_user`` 挂在文档上传/删除路径上。
3. **只缓存干净的最终回答。** 出过错、被护栏拦过、或者中途中断的回答不入缓存。

## 为什么先查精确匹配

同一个问题原样再问一遍是最常见的情况(刷新页面、重新生成),这条路不该花一次
embedding 调用。精确匹配用归一化文本的哈希,零成本;语义匹配才走向量。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from services.telemetry import SpanKind, tracer

logger = logging.getLogger("semantic_cache")

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """归一化到"同一个问题"的粒度:折叠空白、去掉首尾标点、统一大小写。"""
    cleaned = _WHITESPACE.sub(" ", text or "").strip().lower()
    return cleaned.strip("?？!！。.,，、 ")


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


@dataclass(slots=True)
class CacheEntry:
    question: str
    answer: str
    model: str
    # 存下当时是否开着 RAG。不存的话精确匹配键两边会同时代入同一个值，
    # 这个维度就被约掉了——关掉 RAG 后依然会命中带检索的旧答案。
    use_rag: bool = False
    # 生成这条答案时用的提示词版本（``chat_system_rag@v2`` 这种）。
    # 少了这个维度，A/B 提示词就会互相污染：切到 v3 之后第一个问题命中的
    # 还是 v2 的旧答案，看起来"新提示词毫无变化"。
    prompt_ref: str = ""
    embedding: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: float = 0.0

    @property
    def tokens_saved(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class CacheHit:
    entry: CacheEntry
    similarity: float
    exact: bool


class _Store:
    """按用户分桶的进程内存储。

    没有用 Redis:向量比对要拿到全部候选,Redis 里存向量再逐个取回反而更慢,
    而且这个项目的规模用不上跨进程共享。真要横向扩展应该换成向量库,
    而不是把 list 塞进 Redis。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, list[CacheEntry]] = {}

    def bucket(self, user_id: str) -> list[CacheEntry]:
        return self._buckets.setdefault(user_id, [])

    def add(self, user_id: str, entry: CacheEntry, max_entries: int) -> None:
        bucket = self.bucket(user_id)
        bucket.append(entry)
        # 超量时丢最旧的,保持有界。命中率优化留给真正的向量库。
        if len(bucket) > max_entries:
            del bucket[: len(bucket) - max_entries]

    def drop(self, user_id: str) -> int:
        removed = len(self._buckets.get(user_id, []))
        self._buckets.pop(user_id, None)
        return removed

    def clear(self) -> None:
        self._buckets.clear()

    def prune(self, user_id: str, ttl: float) -> None:
        if ttl <= 0:
            return
        cutoff = time.time() - ttl
        bucket = self.bucket(user_id)
        self._buckets[user_id] = [e for e in bucket if e.created_at >= cutoff]


class SemanticCache:
    def __init__(self) -> None:
        self._store = _Store()
        self._embedder: Any | None = None
        # 命中统计只用于面板展示，进程重启即归零
        self.hits = 0
        self.misses = 0
        self.tokens_saved = 0

    @property
    def enabled(self) -> bool:
        return settings.SEMANTIC_CACHE_ENABLED

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            from services.embedding_service import EmbeddingService

            self._embedder = EmbeddingService()
        return self._embedder

    @staticmethod
    def _key(
        user_id: str, question: str, model: str, use_rag: bool, prompt_ref: str
    ) -> str:
        """精确匹配键。带上 model / use_rag / 提示词版本:
        这三者任一不同,答案就不该复用。"""
        payload = json.dumps(
            {
                "u": user_id,
                "q": _normalize(question),
                "m": model,
                "rag": use_rag,
                "p": prompt_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def lookup(
        self,
        user_id: str,
        question: str,
        model: str,
        use_rag: bool,
        prompt_ref: str = "",
    ) -> CacheHit | None:
        """查缓存。先精确匹配（零成本），再语义匹配（一次 embedding 调用）。"""
        if not self.enabled or not question.strip():
            return None

        self._store.prune(user_id, settings.SEMANTIC_CACHE_TTL_SECONDS)
        bucket = self._store.bucket(user_id)
        if not bucket:
            self.misses += 1
            return None

        async with tracer.span("cache.lookup", SpanKind.AGENT) as span:
            key = self._key(user_id, question, model, use_rag, prompt_ref)
            for entry in reversed(bucket):
                if (
                    self._key(
                        user_id,
                        entry.question,
                        entry.model,
                        entry.use_rag,
                        entry.prompt_ref,
                    )
                    == key
                ):
                    span.set(hit=True, exact=True, similarity=1.0)
                    self.hits += 1
                    self.tokens_saved += entry.tokens_saved
                    return CacheHit(entry=entry, similarity=1.0, exact=True)

            threshold = settings.SEMANTIC_CACHE_THRESHOLD
            try:
                vector = await self._get_embedder().embed_query(question)
            except Exception as exc:
                # 缓存查不了不该影响回答，直接当未命中
                logger.warning("cache embedding failed: %s", type(exc).__name__)
                span.set(hit=False, error="embedding")
                self.misses += 1
                return None

            best: CacheEntry | None = None
            best_score = 0.0
            for entry in bucket:
                if (
                    entry.model != model
                    or entry.use_rag != use_rag
                    or entry.prompt_ref != prompt_ref
                ):
                    continue
                if not entry.embedding:
                    continue
                score = _cosine(vector, entry.embedding)
                if score > best_score:
                    best, best_score = entry, score

            span.set(hit=best is not None and best_score >= threshold,
                     exact=False,
                     similarity=round(best_score, 4),
                     threshold=threshold,
                     candidates=len(bucket))
            if best is not None and best_score >= threshold:
                self.hits += 1
                self.tokens_saved += best.tokens_saved
                return CacheHit(entry=best, similarity=best_score, exact=False)

        self.misses += 1
        return None

    async def store(
        self,
        user_id: str,
        question: str,
        answer: str,
        model: str,
        use_rag: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        prompt_ref: str = "",
    ) -> None:
        """写入缓存。embedding 拿不到就只留精确匹配能力，不因此丢弃这条。"""
        if not self.enabled or not question.strip() or not answer.strip():
            return

        vector: list[float] = []
        try:
            vector = await self._get_embedder().embed_query(question)
        except Exception as exc:
            logger.warning("cache store embedding failed: %s", type(exc).__name__)

        self._store.add(
            user_id,
            CacheEntry(
                question=question,
                answer=answer,
                model=model,
                use_rag=use_rag,
                prompt_ref=prompt_ref,
                embedding=vector,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                created_at=time.time(),
            ),
            settings.SEMANTIC_CACHE_MAX_ENTRIES,
        )

    def invalidate_user(self, user_id: str) -> int:
        """知识库变了就把该用户的缓存全清掉。

        没有做"只清受影响的问题"——判断哪些答案依赖被删的文档需要记录每条回答用了
        哪些分块，收益不值这个复杂度。整桶清掉是保守但正确的做法。
        """
        removed = self._store.drop(user_id)
        if removed:
            logger.info("semantic cache invalidated for user: %s entries", removed)
        return removed

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            # 没有任何查询时给 None，不要显示成 0% 命中率
            "hitRate": (self.hits / total) if total else None,
            "tokensSaved": self.tokens_saved,
            "threshold": settings.SEMANTIC_CACHE_THRESHOLD,
        }

    def reset(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
        self.tokens_saved = 0


semantic_cache = SemanticCache()
