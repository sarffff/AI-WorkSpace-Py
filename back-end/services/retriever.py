"""混合检索管线。

一次检索的完整链路：

    查询改写(可选) -> 稠密召回 + BM25 召回 -> RRF 融合 -> 重排(可选) -> 邻域扩展

每一环都能单独关掉，方便对照观察各自的贡献。默认只开"免费"的部分：混合召回、
RRF、邻域扩展；改写和重排各多一次模型调用，默认关闭，按需在配置里打开。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from models import Document, DocumentChunk
from services.embedding_service import EmbeddingService
from services.telemetry import SpanKind, tracer
from services.retrieval_index import (
    BM25Index,
    VectorIndex,
    get_user_indexes,
    reciprocal_rank_fusion,
)

logger = logging.getLogger("retriever")

_JSON_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)


@dataclass(slots=True)
class RetrievedChunk:
    """一条检索结果。经过邻域扩展后可能覆盖多个相邻分块。"""

    document_id: str
    document_name: str
    chunk_index: int
    content: str
    fusion_score: float
    dense_score: float | None
    channels: tuple[str, ...]
    chunk_range: tuple[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "chunk_index": self.chunk_index,
            "chunk_range": list(self.chunk_range),
            "content": self.content,
            "score": round(self.dense_score, 4) if self.dense_score is not None else None,
            "fusion_score": round(self.fusion_score, 6),
            "channels": list(self.channels),
        }


def format_context(chunks: list[RetrievedChunk]) -> str:
    """把检索结果格式化成喂给模型的参考内容。"""
    if not chunks:
        return ""
    parts = ["以下是知识库中与当前问题相关的参考内容：\n"]
    for position, chunk in enumerate(chunks, start=1):
        low, high = chunk.chunk_range
        span = f"{low}" if low == high else f"{low}-{high}"
        relevance = "-" if chunk.dense_score is None else f"{chunk.dense_score:.4f}"
        parts.append(
            f"【参考 {position}】来源: {chunk.document_name}，"
            f"document_id: {chunk.document_id}，分块: {span}，"
            f"相关度: {relevance}\n{chunk.content}\n"
        )
    return "\n".join(parts)


def _parse_json_array(text: str) -> list[Any]:
    """从模型输出里抠出第一个 JSON 数组。模型爱加解释性文字，直接 loads 会炸。"""
    if not text:
        return []
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


class HybridRetriever:
    def __init__(self, embedding=None, model_adapter=None) -> None:
        self._embedding = embedding or EmbeddingService()
        self._model_adapter = model_adapter

    def _get_model_adapter(self):
        """改写/重排才需要模型，按需构建以免普通检索也去建客户端。"""
        if self._model_adapter is None:
            from services.model_adapter import OpenAICompatibleAdapter

            self._model_adapter = OpenAICompatibleAdapter()
        return self._model_adapter

    @staticmethod
    def _load_rows(db: Session, user_id: str) -> list[tuple[DocumentChunk, Document]]:
        return (
            db.query(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .filter(Document.user_id == user_id, Document.status == "indexed")
            .order_by(DocumentChunk.id.asc())
            .all()
        )

    async def retrieve(
        self, db: Session, user_id: str, query: str, top_k: int = 5
    ) -> list[RetrievedChunk]:
        """检索入口。埋点记下本次用的是哪套开关组合与命中情况。

        这些属性就是配置扫描能落地的前提：同一批问题跑不同变体，
        事后按 span 的 hybrid / rerank / multi_query 分组对比即可。
        """
        async with tracer.span(
            "retrieval.hybrid" if settings.RAG_HYBRID else "retrieval.dense",
            SpanKind.RETRIEVAL,
            top_k=top_k,
            hybrid=settings.RAG_HYBRID,
            multi_query=settings.RAG_MULTI_QUERY,
            rerank=settings.RAG_RERANK,
            context_window=settings.RAG_CONTEXT_WINDOW,
        ) as span:
            results = await self._retrieve(db, user_id, query, top_k)
            span.set(
                hits=len(results),
                channels=sorted({c for r in results for c in r.channels}) or None,
                top_score=max(
                    (r.dense_score for r in results if r.dense_score is not None),
                    default=None,
                ),
            )
            return results

    async def _retrieve(
        self, db: Session, user_id: str, query: str, top_k: int = 5
    ) -> list[RetrievedChunk]:
        rows = self._load_rows(db, user_id)
        if not rows:
            return []

        chunks = [chunk for chunk, _document in rows]
        by_id = {chunk.id: (chunk, document) for chunk, document in rows}
        indexes = get_user_indexes(user_id)
        indexes.vector.build_if_stale(chunks)
        if settings.RAG_HYBRID:
            indexes.bm25.build_if_stale(chunks)

        safe_top_k = max(1, min(top_k, 20))
        per_channel = max(safe_top_k, settings.RAG_CANDIDATES_PER_CHANNEL)
        queries = await self._expand_queries(query)

        rankings: list[list[str]] = []
        dense_scores: dict[str, float] = {}
        channels: dict[str, set[str]] = {}

        for text in queries:
            dense = await self._dense_search(text, indexes.vector, per_channel)
            if dense:
                rankings.append([chunk_id for _score, chunk_id in dense])
                for score, chunk_id in dense:
                    dense_scores[chunk_id] = max(dense_scores.get(chunk_id, -1.0), score)
                    channels.setdefault(chunk_id, set()).add("dense")
            if settings.RAG_HYBRID:
                sparse = self._sparse_search(text, indexes.bm25, per_channel)
                if sparse:
                    rankings.append([chunk_id for _score, chunk_id in sparse])
                    for _score, chunk_id in sparse:
                        channels.setdefault(chunk_id, set()).add("sparse")

        if not rankings:
            return []

        fused = [
            (chunk_id, score)
            for chunk_id, score in reciprocal_rank_fusion(rankings)
            if chunk_id in by_id
        ]
        if not fused:
            return []

        ordered_ids = await self._maybe_rerank(query, [cid for cid, _ in fused], by_id)
        fusion_scores = dict(fused)
        selected = ordered_ids[:safe_top_k]
        return self._expand(selected, fusion_scores, dense_scores, channels, by_id)

    async def _dense_search(
        self, query: str, index: VectorIndex, limit: int
    ) -> list[tuple[float, str]]:
        try:
            vector = await self._embedding.embed_query(query)
        except Exception as exc:
            logger.warning("dense channel unavailable: %s", type(exc).__name__)
            return []
        if not vector:
            return []
        # 相关度下限只作用于稠密通道：余弦相似度有绝对含义，BM25 分数没有。
        return [
            (score, chunk_id)
            for score, chunk_id in index.search(vector, top_k=limit)
            if score >= settings.RAG_MIN_SCORE
        ]

    @staticmethod
    def _sparse_search(
        query: str, index: BM25Index, limit: int
    ) -> list[tuple[float, str]]:
        try:
            return index.search(query, top_k=limit)
        except Exception as exc:
            logger.warning("sparse channel unavailable: %s", type(exc).__name__)
            return []

    async def _expand_queries(self, query: str) -> list[str]:
        """多查询改写：用户提问的措辞和文档措辞常常不一致，多个变体能显著提召回。"""
        if not settings.RAG_MULTI_QUERY:
            return [query]

        count = max(1, settings.RAG_MULTI_QUERY_COUNT)
        prompt = (
            f"把下面的检索问题改写成 {count} 个不同措辞的检索查询，"
            "覆盖同义词、专业术语和更具体的表述。"
            '只输出 JSON 字符串数组，例如 ["查询1", "查询2"]，不要任何解释。\n\n'
            f"问题：{query}"
        )
        try:
            completion = await self._get_model_adapter().complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=settings.LLM_MODEL,
                temperature=0.3,
                max_tokens=256,
                purpose="query_rewrite",
            )
        except Exception as exc:
            logger.warning("query rewrite failed: %s", type(exc).__name__)
            return [query]

        variants = [
            item.strip()
            for item in _parse_json_array(completion.content)
            if isinstance(item, str) and item.strip() and item.strip() != query
        ]
        return [query, *variants[:count]]

    async def _maybe_rerank(
        self,
        query: str,
        ordered_ids: list[str],
        by_id: dict[str, tuple[DocumentChunk, Document]],
    ) -> list[str]:
        """LLM listwise 重排。

        RRF 只看排名，无法判断"字面命中但语义无关"。让模型直接读候选片段做一次
        重排，代价是一次额外调用。生产上更常用专门的 cross-encoder rerank 服务，
        这里用 LLM 顶上，好处是不引入新依赖。
        """
        if not settings.RAG_RERANK or len(ordered_ids) < 2:
            return ordered_ids

        limit = max(2, settings.RAG_RERANK_CANDIDATES)
        candidates = ordered_ids[:limit]
        tail = ordered_ids[limit:]

        passages = []
        for position, chunk_id in enumerate(candidates, start=1):
            chunk, _document = by_id[chunk_id]
            snippet = (chunk.content or "")[: settings.RAG_RERANK_SNIPPET_CHARS]
            passages.append(f"[{position}] {snippet}")

        prompt = (
            "下面是候选参考片段。请按与问题的相关程度从高到低排序，"
            "完全不相关的片段直接省略。只输出片段编号组成的 JSON 数组，"
            "例如 [3, 1, 5]，不要任何解释。\n\n"
            f"问题：{query}\n\n" + "\n\n".join(passages)
        )
        try:
            completion = await self._get_model_adapter().complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=settings.LLM_MODEL,
                temperature=0.0,
                max_tokens=256,
                purpose="rerank",
            )
        except Exception as exc:
            logger.warning("rerank failed: %s", type(exc).__name__)
            return ordered_ids

        seen: set[str] = set()
        reranked: list[str] = []
        for value in _parse_json_array(completion.content):
            if not isinstance(value, int) or not 1 <= value <= len(candidates):
                continue
            chunk_id = candidates[value - 1]
            if chunk_id not in seen:
                seen.add(chunk_id)
                reranked.append(chunk_id)
        if not reranked:
            return ordered_ids
        # 模型漏掉的候选保持原相对顺序追加在后面，避免重排把召回变成筛除
        rest = [chunk_id for chunk_id in candidates if chunk_id not in seen]
        return reranked + rest + tail

    @staticmethod
    def _expand(
        selected: list[str],
        fusion_scores: dict[str, float],
        dense_scores: dict[str, float],
        channels: dict[str, set[str]],
        by_id: dict[str, tuple[DocumentChunk, Document]],
    ) -> list[RetrievedChunk]:
        """邻域扩展 + 合并。

        小块利于命中，大块利于回答——检索用小块，喂给模型时把命中块前后的相邻块
        一起带上，补回被切断的上下文。这也是不做跨段重叠存储的前提：重复内容
        在检索时按需拼装，而不是在入库时冗余存三份。
        重叠的邻域会合并成一条，避免同一段文字在参考内容里出现两遍。
        """
        window = max(0, settings.RAG_CONTEXT_WINDOW)
        by_document: dict[str, dict[int, DocumentChunk]] = {}
        for chunk, document in by_id.values():
            by_document.setdefault(document.id, {})[chunk.chunk_index] = chunk

        groups: dict[str, list[dict[str, Any]]] = {}
        for chunk_id in selected:
            chunk, document = by_id[chunk_id]
            available = by_document.get(document.id, {})
            low = max(min(available, default=chunk.chunk_index), chunk.chunk_index - window)
            high = min(max(available, default=chunk.chunk_index), chunk.chunk_index + window)
            groups.setdefault(document.id, []).append(
                {
                    "document": document,
                    "anchor": chunk.chunk_index,
                    "low": low,
                    "high": high,
                    "fusion": fusion_scores.get(chunk_id, 0.0),
                    "dense": dense_scores.get(chunk_id),
                    "channels": set(channels.get(chunk_id, ())),
                }
            )

        merged: list[dict[str, Any]] = []
        for entries in groups.values():
            entries.sort(key=lambda entry: entry["low"])
            for entry in entries:
                if merged and merged[-1]["document"].id == entry["document"].id and entry["low"] <= merged[-1]["high"]:
                    previous = merged[-1]
                    previous["high"] = max(previous["high"], entry["high"])
                    previous["channels"] |= entry["channels"]
                    if entry["fusion"] > previous["fusion"]:
                        previous["fusion"] = entry["fusion"]
                        previous["anchor"] = entry["anchor"]
                    if entry["dense"] is not None:
                        previous["dense"] = max(previous["dense"] or -1.0, entry["dense"])
                    continue
                merged.append(entry)

        merged.sort(key=lambda entry: -entry["fusion"])

        results: list[RetrievedChunk] = []
        for entry in merged:
            document = entry["document"]
            available = by_document.get(document.id, {})
            body = "\n\n".join(
                available[index].content
                for index in range(entry["low"], entry["high"] + 1)
                if index in available
            )
            if not body:
                continue
            results.append(
                RetrievedChunk(
                    document_id=document.id,
                    document_name=document.name,
                    chunk_index=entry["anchor"],
                    content=body,
                    fusion_score=entry["fusion"],
                    dense_score=entry["dense"],
                    channels=tuple(sorted(entry["channels"])),
                    chunk_range=(entry["low"], entry["high"]),
                )
            )
        return results
