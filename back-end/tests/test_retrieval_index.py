"""BM25 稀疏索引、向量索引与 RRF 融合。"""
from __future__ import annotations

from config import settings
from models import DocumentChunk
from services.embedding_service import EmbeddingService
from services.retrieval_index import (
    BM25Index,
    VectorIndex,
    reciprocal_rank_fusion,
    tokenize,
)


def _chunk(chunk_id: str, content: str, vector: list[float] | None = None):
    return DocumentChunk(
        id=chunk_id,
        document_id="d1",
        content=content,
        chunk_index=0,
        embedding=(
            EmbeddingService.serialize(vector, model=settings.EMBEDDING_MODEL)
            if vector
            else None
        ),
    )


def test_tokenize_splits_cjk_into_bigrams():
    assert tokenize("安装指南") == ["安装", "装指", "指南"]


def test_tokenize_keeps_single_cjk_char():
    assert tokenize("我") == ["我"]


def test_tokenize_lowercases_latin_and_keeps_digits():
    assert tokenize("Install Python3") == ["install", "python3"]


def test_tokenize_mixes_both_scripts():
    tokens = tokenize("安装 Python")

    assert "python" in tokens
    assert "安装" in tokens


def test_bm25_ranks_exact_term_match_first():
    index = BM25Index()
    index.build_if_stale(
        [
            _chunk("a", "预算审批流程说明"),
            _chunk("b", "员工入职手册"),
            _chunk("c", "报销与预算无关的杂项"),
        ]
    )

    results = index.search("预算", top_k=5)

    assert [chunk_id for _score, chunk_id in results] == ["a", "c"]


def test_bm25_returns_nothing_when_no_term_matches():
    index = BM25Index()
    index.build_if_stale([_chunk("a", "员工入职手册")])

    assert index.search("量子纠缠", top_k=5) == []
    assert index.search("", top_k=5) == []


def test_bm25_penalises_longer_documents():
    """长度归一化：同样命中一次，短文档更相关。"""
    index = BM25Index()
    index.build_if_stale(
        [
            _chunk("short", "预算"),
            _chunk("long", "预算" + "其他内容" * 50),
        ]
    )

    results = dict((chunk_id, score) for score, chunk_id in index.search("预算", 5))

    assert results["short"] > results["long"]


def test_bm25_rebuilds_when_content_changes():
    index = BM25Index()
    index.build_if_stale([_chunk("a", "苹果香蕉")])
    assert index.search("苹果香蕉", 5)

    index.build_if_stale([_chunk("a", "钢铁水泥")])

    assert index.search("苹果香蕉", 5) == []
    assert index.search("钢铁水泥", 5)


def test_rrf_prefers_items_hit_by_multiple_channels():
    """b 在两路都排第二，a 只在一路排第一——多路共识胜出。"""
    fused = reciprocal_rank_fusion([["a", "b"], ["c", "b"]])

    assert fused[0][0] == "b"


def test_rrf_handles_empty_input():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []


def test_rrf_scores_decay_with_rank():
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"]]))

    assert fused["a"] > fused["b"] > fused["c"]


def test_vector_index_returns_cosine_scores():
    index = VectorIndex()
    index.build_if_stale(
        [_chunk("a", "x", [1.0, 0.0]), _chunk("b", "y", [0.0, 1.0])]
    )

    results = index.search([1.0, 0.0], top_k=2)

    assert results[0][1] == "a"
    assert round(results[0][0], 4) == 1.0
    assert round(results[1][0], 4) == 0.0


def test_vector_index_skips_chunks_without_embedding():
    index = VectorIndex()
    index.build_if_stale([_chunk("a", "x"), _chunk("b", "y", [1.0, 0.0])])

    assert [chunk_id for _score, chunk_id in index.search([1.0, 0.0], 5)] == ["b"]


def test_vector_index_skips_mismatched_dimensions():
    index = VectorIndex()
    index.build_if_stale(
        [_chunk("a", "x", [1.0, 0.0]), _chunk("b", "y", [1.0, 0.0, 0.0])]
    )

    assert [chunk_id for _score, chunk_id in index.search([1.0, 0.0], 5)] == ["a"]


def test_vector_index_rejects_query_with_wrong_dimension():
    index = VectorIndex()
    index.build_if_stale([_chunk("a", "x", [1.0, 0.0])])

    assert index.search([1.0, 0.0, 0.0], 5) == []
