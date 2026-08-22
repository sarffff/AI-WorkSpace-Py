"""混合检索管线。

一次检索的完整链路：

    查询路由(可选) -> HyDE(可选) -> 查询改写(可选)
      -> 稠密召回 + BM25 召回 -> 带权 RRF 融合 -> 重排(可选) -> 邻域扩展

每一环都能单独关掉，方便对照观察各自的贡献。默认只开"免费"的部分：混合召回、
RRF、邻域扩展；路由、HyDE、改写、重排各多一次模型调用，默认关闭，按需在配置里打开。

**两条通道拿到的查询可以不一样，这是有意的。** HyDE 的假答案只喂稠密通道，
BM25 永远用原始 query：假答案里的专有名词与编号全是模型编的，拿它做字面匹配
只会命中一堆无关内容，等于亲手废掉稀疏通道。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, defer

from config import settings
from models import Document, DocumentChunk
from services import structured
from services import vector_store
from services.embedding_service import EmbeddingService
from services.guardrails import ScanReport, guard, mask_markup
from services.rerank import RerankError, rerank_client
from services.telemetry import SpanKind, tracer
from services.retrieval_index import (
    BM25Index,
    VectorIndex,
    get_scope_indexes,
    indexes_fresh,
    reciprocal_rank_fusion,
    signature_from_ids,
)

logger = logging.getLogger("retriever")

# 通道标签。带权 RRF 要靠它把权重对上通道，字符串写错不会报错、只会静默错配，
# 所以集中在这里定义一次。
_DENSE = "dense"
_SPARSE = "sparse"


def rerank_mode() -> str:
    """当前生效的重排方式。

    ``RAG_RERANK_MODE`` 留空时回落到布尔量 ``RAG_RERANK``，于是既有的 ``rerank``
    变体和 ``test_retriever.py`` 都不用改一个字——旧开关仍然是有效入口。
    """
    mode = (settings.RAG_RERANK_MODE or "").strip().lower()
    if mode in ("off", "llm", "api"):
        return mode
    return "llm" if settings.RAG_RERANK else "off"


def _warn_degraded(stage: str, report, fallback: str) -> None:
    """检索增强降级时留一条日志。

    这四处(路由/HyDE/多查询/重排)的降级路径本身是对的——增强失败不该让回答
    失败。问题在于**降级是无声的**:``result is None`` 就静默走 fallback,报告里
    只看到"这个技术没有增益"。2026-08-22 实测四处全部 100% 降级(预算被思考
    吃光),而 eval 里对应的变体与 baseline 逐位相同,持续了很久没人发现。

    所以这条日志的作用不是排错细节,而是让"我配了但它没生效"这件事**可见**。
    ``budget_exhausted`` 时 structured 层已经喊过一次预算问题,这里补上业务后果:
    哪个阶段降级了、退成了什么。
    """
    logger.warning(
        "%s degraded (attempts=%s failures=%s finish_reason=%s) → %s",
        stage,
        report.attempts,
        report.failures,
        report.finish_reason,
        fallback,
    )


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
    """把检索结果格式化成喂给模型的参考内容。

    每块正文单独过一遍护栏再拼表头——顺序反了就会把我们自己加的【参考 N】表头
    当成伪造表头屏蔽掉。拼好之后整段用随机定界符围一次,让模型能分清哪些是数据。
    """
    if not chunks:
        return ""
    parts = ["以下是知识库中与当前问题相关的参考内容：\n"]
    report = ScanReport()
    for position, chunk in enumerate(chunks, start=1):
        low, high = chunk.chunk_range
        span = f"{low}" if low == high else f"{low}-{high}"
        relevance = "-" if chunk.dense_score is None else f"{chunk.dense_score:.4f}"
        body, chunk_report = guard.sanitize(chunk.content)
        report = report.merge(chunk_report)
        parts.append(
            f"【参考 {position}】来源: {mask_markup(chunk.document_name)}，"
            f"document_id: {chunk.document_id}，分块: {span}，"
            f"相关度: {relevance}\n{body}\n"
        )
    guard.record(report, kind="retrieval")
    return guard.fence("\n".join(parts), label="参考资料")


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
    def _load_chunk_ids(db: Session, workspace_id: str) -> list[str]:
        """只查 id 列的轻量查询,用于算索引签名判断是否需要重建。"""
        rows = (
            db.query(DocumentChunk.id)
            .join(Document, DocumentChunk.document_id == Document.id)
            .filter(Document.workspace_id == workspace_id, Document.status == "indexed")
            .order_by(DocumentChunk.id.asc())
            .all()
        )
        return [row[0] for row in rows]

    @staticmethod
    def _load_rows(
        db: Session, workspace_id: str, *, with_embeddings: bool
    ) -> list[tuple[DocumentChunk, Document]]:
        query = (
            db.query(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .filter(Document.workspace_id == workspace_id, Document.status == "indexed")
            .order_by(DocumentChunk.id.asc())
        )
        if not with_embeddings:
            # embedding 是整张表最大的列(每块几 KB 的 JSON);索引新鲜时它
            # 只对重建有用,defer 掉就能把热路径的传输量降一个数量级
            query = query.options(defer(DocumentChunk.embedding))
        return query.all()

    async def retrieve(
        self, db: Session, workspace_id: str, query: str, top_k: int = 5
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
            rerank=rerank_mode(),
            hyde=settings.RAG_HYDE,
            query_route=settings.RAG_QUERY_ROUTE,
            context_window=settings.RAG_CONTEXT_WINDOW,
        ) as span:
            results = await self._retrieve(db, workspace_id, query, top_k, span)
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
        self,
        db: Session,
        workspace_id: str,
        query: str,
        top_k: int = 5,
        span: Any = None,
    ) -> list[RetrievedChunk]:
        # 先用 id 集合签名判断索引是否新鲜:新鲜则跳过 embedding 大字段的
        # 拉取与重建,热路径的检索成本不再随库规模线性增长
        chunk_ids = self._load_chunk_ids(db, workspace_id)
        if not chunk_ids:
            return []
        signature = signature_from_ids(chunk_ids)
        fresh = indexes_fresh(workspace_id, signature)
        # Qdrant 后端下稠密通道不再需要 embedding 列——向量在 Qdrant 里,MySQL 这份
        # 只是给回填脚本用的。BM25 只要 content,所以整个热路径都不必碰那个大字段。
        store = vector_store.get_store()
        on_qdrant = vector_store.uses_qdrant()
        rows = self._load_rows(
            db, workspace_id, with_embeddings=not fresh and not on_qdrant
        )

        chunks = [chunk for chunk, _document in rows]
        by_id = {chunk.id: (chunk, document) for chunk, document in rows}
        indexes = get_scope_indexes(workspace_id)
        if not on_qdrant:
            indexes.vector.build_if_stale(chunks, signature)
        if settings.RAG_HYBRID:
            indexes.bm25.build_if_stale(chunks, signature)

        safe_top_k = max(1, min(top_k, 20))
        per_channel = max(safe_top_k, settings.RAG_CANDIDATES_PER_CHANNEL)

        route = await self._route_query(query)
        if span is not None and route is not None:
            # 记进 span 才能事后和 eval 的 probe 标注对一遍——那是这个分类器
            # 现成的标注集,不必凭感觉判断路由准不准
            span.set(route_intent=route.intent)
        dense_weight = route.dense_weight if route else settings.RAG_RRF_DENSE_WEIGHT
        sparse_weight = route.sparse_weight if route else settings.RAG_RRF_SPARSE_WEIGHT

        # 稠密通道用的查询可以和字面通道不一样:HyDE 只作用于前者
        dense_seed = await self._hyde_query(query)
        if span is not None and dense_seed != query:
            span.set(hyde_applied=True)

        queries = await self._expand_queries(query)
        dense_queries = (
            [dense_seed, *queries[1:]] if dense_seed != query else list(queries)
        )

        rankings: list[list[str]] = []
        weights: list[float] = []
        dense_scores: dict[str, float] = {}
        channels: dict[str, set[str]] = {}

        for position, text in enumerate(queries):
            dense = await self._dense_search(
                dense_queries[position], store, workspace_id, per_channel
            )
            if dense:
                rankings.append([chunk_id for _score, chunk_id in dense])
                weights.append(dense_weight)
                for score, chunk_id in dense:
                    dense_scores[chunk_id] = max(dense_scores.get(chunk_id, -1.0), score)
                    channels.setdefault(chunk_id, set()).add(_DENSE)
            if settings.RAG_HYBRID:
                sparse = self._sparse_search(text, indexes.bm25, per_channel)
                if sparse:
                    rankings.append([chunk_id for _score, chunk_id in sparse])
                    weights.append(sparse_weight)
                    for _score, chunk_id in sparse:
                        channels.setdefault(chunk_id, set()).add(_SPARSE)

        if not rankings:
            return []

        fused = [
            (chunk_id, score)
            for chunk_id, score in reciprocal_rank_fusion(rankings, weights=weights)
            if chunk_id in by_id
        ]
        if not fused:
            return []

        ordered_ids = await self._maybe_rerank(query, [cid for cid, _ in fused], by_id)
        fusion_scores = dict(fused)
        selected = ordered_ids[:safe_top_k]
        return self._expand(selected, fusion_scores, dense_scores, channels, by_id)

    async def _route_query(self, query: str) -> structured.QueryRoute | None:
        """判断查询偏字面还是偏语义。失败返回 None（调用方用默认权重）。"""
        if not settings.RAG_QUERY_ROUTE:
            return None
        prompt = (
            "判断下面这个检索问题该偏重哪种召回方式，只输出 JSON 对象。\n"
            "- lexical：包含精确的编号、错误码、API 名、专有名词，字面匹配更重要\n"
            "- semantic：改述式提问，措辞和文档大概率不同，语义相似更重要\n"
            "- mixed：两者都有\n"
            '格式：{"intent": "lexical"}，不要任何解释。\n\n'
            f"问题：{query}"
        )
        result, report = await structured.request_structured(
            self._get_model_adapter(),
            schema=structured.QueryRoute,
            prompt=prompt,
            model=settings.utility_model,
            purpose="query_route",
            array=False,
            temperature=0.0,
            max_tokens=settings.RAG_ROUTE_MAX_TOKENS,
        )
        if result is None:
            _warn_degraded("query_route", report, "两路 RRF 权重都用默认值")
        return result

    async def _hyde_query(self, query: str) -> str:
        """让模型编一段假答案当稠密检索的输入。失败就用原查询。

        为什么这招有用：用户的问题和文档的措辞常常不在同一个语域——"报销要几天"
        对应的文档写的是"费用审批时限"。稠密检索比的是问题向量和文档向量，而一段
        假答案的措辞天然更接近文档，等于把查询先搬到文档所在的那片向量空间。

        假答案的**事实正确性完全无关**：它不进上下文、不给用户看，只用来向量化。
        """
        if not settings.RAG_HYDE:
            return query
        prompt = (
            "为下面这个问题写一段听起来像是从公司内部文档里摘出来的答案，"
            "两三句话，用文档式的书面措辞和术语。不确定的细节可以编，"
            "这段文字只用于检索、不会展示给任何人。\n"
            '只输出 JSON：{"answer": "..."}，不要任何解释。\n\n'
            f"问题：{query}"
        )
        result, report = await structured.request_structured(
            self._get_model_adapter(),
            schema=structured.HypotheticalAnswer,
            prompt=prompt,
            model=settings.utility_model,
            purpose="hyde",
            array=False,
            temperature=0.3,
            max_tokens=settings.RAG_HYDE_MAX_TOKENS,
        )
        if result is None:
            _warn_degraded("hyde", report, "稠密通道用原查询")
            return query
        # 原查询拼在前面而不是整个替换掉：假答案可能整段跑偏，留着原查询
        # 至少保证向量里还有用户真正问的那件事
        return f"{query}\n{result.answer}"

    async def _dense_search(
        self, query: str, store: Any, workspace_id: str, limit: int
    ) -> list[tuple[float, str]]:
        try:
            vector = await self._embedding.embed_query(query)
        except Exception as exc:
            logger.warning("dense channel unavailable: %s", type(exc).__name__)
            return []
        if not vector:
            return []
        try:
            hits = await store.search(workspace_id, vector, limit)
        except Exception as exc:
            # 向量库故障不该让整次检索失败:BM25 那一路还在,退化成纯稀疏检索
            # 仍然能答对不少问题,比抛 500 好
            logger.warning("vector store search failed: %s", type(exc).__name__)
            return []
        # 相关度下限只作用于稠密通道：余弦相似度有绝对含义，BM25 分数没有。
        # 两个后端的分数都是余弦相似度（Qdrant 侧用 Distance.COSINE），
        # 所以这个阈值在两边含义相同。
        return [(score, chunk_id) for score, chunk_id in hits if score >= settings.RAG_MIN_SCORE]

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
        result, report = await structured.request_structured(
            self._get_model_adapter(),
            schema=structured.QueryVariants,
            prompt=prompt,
            model=settings.utility_model,
            purpose="query_rewrite",
            array=True,
            temperature=0.3,
            max_tokens=settings.RAG_MULTI_QUERY_MAX_TOKENS,
        )
        if result is None:
            # 改写是增强不是依赖:失败就用原查询单路召回
            _warn_degraded("query_rewrite", report, "退回单路召回")
            return [query]

        variants = [item for item in result.items if item != query]
        return [query, *variants[:count]]

    async def _maybe_rerank(
        self,
        query: str,
        ordered_ids: list[str],
        by_id: dict[str, tuple[DocumentChunk, Document]],
    ) -> list[str]:
        """重排。按 ``RAG_RERANK_MODE`` 分派到 cross-encoder 或 LLM listwise。

        RRF 只看排名，无法判断"字面命中但语义无关"，所以精排这一步有真实价值。
        两种实现的区别是学习上的重点：

        - ``api`` 走专用 cross-encoder：query 与 document 拼在一起过一遍模型，
          输出标量相关度。它比稠密检索准的原因就在这里——稠密是 bi-encoder，
          两侧各自独立编码，编码时从未见过对方。代价是没法预先索引，只能精排。
        - ``llm`` 让通用模型输出一个编号顺序。留着当对照组，用来量前者好多少。
        """
        mode = rerank_mode()
        if mode == "off" or len(ordered_ids) < 2:
            return ordered_ids

        limit = max(2, settings.RAG_RERANK_CANDIDATES)
        candidates = ordered_ids[:limit]
        tail = ordered_ids[limit:]
        snippets = [
            (by_id[chunk_id][0].content or "")[: settings.RAG_RERANK_SNIPPET_CHARS]
            for chunk_id in candidates
        ]

        if mode == "api":
            reranked = await self._rerank_via_api(query, candidates, snippets)
        else:
            reranked = await self._rerank_via_llm(query, candidates, snippets)

        if not reranked:
            # 重排失败就用融合序。这一步只改顺序不改召回集合,退回去是安全的
            return ordered_ids
        # 模型漏掉的候选保持原相对顺序追加在后面，避免重排把召回变成筛除
        seen = set(reranked)
        rest = [chunk_id for chunk_id in candidates if chunk_id not in seen]
        return reranked + rest + tail

    async def _rerank_via_api(
        self, query: str, candidates: list[str], snippets: list[str]
    ) -> list[str]:
        """专用 rerank 接口。未配置或请求失败都退回融合序。"""
        if not rerank_client.configured:
            # 不静默退回 llm：那会让报告里的 rerank=api 与实际跑的东西不一致，
            # 而"api 和 llm 差多少"正是这个变体要量的
            logger.warning("rerank mode=api but the endpoint is not configured")
            return []
        try:
            scored = await rerank_client.rerank(query, snippets)
        except RerankError:
            return []
        return [candidates[index] for index, _score in scored]

    async def _rerank_via_llm(
        self, query: str, candidates: list[str], snippets: list[str]
    ) -> list[str]:
        """LLM listwise 重排。生产上更常用专门的 cross-encoder（见 mode=api），
        这里保留作对照组，好处是不引入新依赖也不需要额外配置。"""
        passages = [
            f"[{position}] {snippet}"
            for position, snippet in enumerate(snippets, start=1)
        ]
        prompt = (
            "下面是候选参考片段。请按与问题的相关程度从高到低排序，"
            "完全不相关的片段直接省略。只输出片段编号组成的 JSON 数组，"
            "例如 [3, 1, 5]，不要任何解释。\n\n"
            f"问题：{query}\n\n" + "\n\n".join(passages)
        )
        result, report = await structured.request_structured(
            self._get_model_adapter(),
            schema=structured.RerankOrder,
            prompt=prompt,
            model=settings.utility_model,
            purpose="rerank",
            array=True,
            temperature=0.0,
            max_tokens=settings.RAG_RERANK_MAX_TOKENS,
        )
        if result is None:
            _warn_degraded("rerank", report, "退回融合序")
            return []

        seen: set[str] = set()
        reranked: list[str] = []
        for value in result.items:
            # 上界在这里查而不是在契约里:能给到几号取决于本次的候选个数,
            # 那是调用方才有的信息。越界编号是模型编的,丢掉就行。
            if value > len(candidates):
                continue
            chunk_id = candidates[value - 1]
            if chunk_id not in seen:
                seen.add(chunk_id)
                reranked.append(chunk_id)
        return reranked

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
        for rank, chunk_id in enumerate(selected):
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
                    # selected 的顺序就是最终排序(融合序,或重排后的序)。
                    # 之前这里按 fusion 分数再排一次,等于把 rerank 的结果整个
                    # 丢掉——重排只剩下的"改顺序"这一半作用。
                    "rank": rank,
                    "fusion": fusion_scores.get(chunk_id, 0.0),
                    "dense": dense_scores.get(chunk_id),
                    "channels": set(channels.get(chunk_id, ())),
                }
            )

        merged: list[dict[str, Any]] = []
        for entries in groups.values():
            # 合并必须按区间起点做:selected 的顺序(融合/重排名次)与分块序号
            # 无关,乱序处理区间会出两种错——漏合并,或者只更新了 high 忘了
            # low,把并集左端的内容静默丢掉。
            entries.sort(key=lambda entry: (entry["low"], entry["rank"]))
            for entry in entries:
                if merged and merged[-1]["document"].id == entry["document"].id and entry["low"] <= merged[-1]["high"]:
                    previous = merged[-1]
                    previous["low"] = min(previous["low"], entry["low"])
                    previous["high"] = max(previous["high"], entry["high"])
                    previous["rank"] = min(previous["rank"], entry["rank"])
                    previous["channels"] |= entry["channels"]
                    if entry["fusion"] > previous["fusion"]:
                        previous["fusion"] = entry["fusion"]
                        previous["anchor"] = entry["anchor"]
                    if entry["dense"] is not None:
                        previous["dense"] = max(previous["dense"] or -1.0, entry["dense"])
                    continue
                merged.append(entry)

        # 输出顺序由各合并段的最小选中名次决定:不重排时等价于融合分数序
        # (RRF 本身按名次排),重排后就是重排的顺序
        merged.sort(key=lambda entry: entry["rank"])

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
