"""混合检索管线：双路召回、RRF 融合、改写、重排、邻域扩展。"""
from __future__ import annotations

import json
from typing import Any

import pytest

from config import settings
from models import Document, DocumentChunk
from services.embedding_service import EmbeddingService
from services.model_adapter import ModelCompletion
from services.retrieval_index import invalidate_scope_indexes
from services.retriever import HybridRetriever, format_context
from conftest import run

USER = "u-retriever"


def _document(document_id: str = "d1", name: str = "notes.md") -> Document:
    return Document(id=document_id, name=name, size=10, user_id=USER, status="indexed")


def _chunk(chunk_id: str, index: int, content: str, vector: list[float], document_id="d1"):
    return DocumentChunk(
        id=chunk_id,
        document_id=document_id,
        content=content,
        chunk_index=index,
        embedding=EmbeddingService.serialize(vector, model=settings.EMBEDDING_MODEL),
    )


class FakeEmbedding:
    """按查询文本返回预设向量，并记录被调用的查询。"""

    def __init__(self, vectors: dict[str, list[float]], default: list[float] | None = None):
        self.vectors = vectors
        self.default = default or [0.0, 0.0]
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return self.vectors.get(query, self.default)


class StaticAdapter:
    """只用于改写/重排的模型替身，按调用顺序返回预设内容。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, *, messages, tools, model, **kwargs) -> ModelCompletion:
        self.prompts.append(messages[-1]["content"])
        content = self._responses.pop(0) if self._responses else ""
        return ModelCompletion(content=content, tool_calls=[])


class StubRetriever(HybridRetriever):
    """绕过数据库：直接喂给管线一组 (chunk, document) 行。"""

    def __init__(self, rows, **kwargs):
        super().__init__(**kwargs)
        self._rows = rows

    def _load_chunk_ids(self, db, user_id):  # type: ignore[override]
        return [chunk.id for chunk, _document in self._rows]

    def _load_rows(self, db, user_id, *, with_embeddings=True):  # type: ignore[override]
        return self._rows


def _corpus() -> list[tuple[DocumentChunk, Document]]:
    document = _document()
    return [
        (_chunk("a0", 0, "预算审批流程 budget", [1.0, 0.0]), document),
        (_chunk("a1", 1, "完全无关的员工餐补说明", [0.0, 1.0]), document),
        (_chunk("a2", 2, "预算又一段补充", [0.9, 0.1]), document),
    ]


@pytest.fixture(autouse=True)
def _isolate_indexes(monkeypatch):
    """索引按用户缓存在进程内，每个用例前后都清掉，避免相互污染。"""
    invalidate_scope_indexes(USER)
    monkeypatch.setattr(settings, "RAG_CONTEXT_WINDOW", 0)
    monkeypatch.setattr(settings, "RAG_MULTI_QUERY", False)
    monkeypatch.setattr(settings, "RAG_RERANK", False)
    monkeypatch.setattr(settings, "RAG_HYBRID", True)
    yield
    invalidate_scope_indexes(USER)


def test_empty_corpus_returns_nothing():
    retriever = StubRetriever([], embedding=FakeEmbedding({}))

    assert run(retriever.retrieve(None, USER, "预算", top_k=5)) == []


def test_dense_and_sparse_channels_are_both_used():
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=3))

    top = results[0]
    # a0 既是稠密最相似，也是唯一字面命中 "budget" 的分块
    assert top.chunk_index == 0
    assert set(top.channels) == {"dense", "sparse"}


def test_sparse_channel_recovers_exact_match_that_dense_misses():
    """稠密向量给 a1 打了最高分，但字面命中的 a0 靠 BM25 被拉回结果里。"""
    embedding = FakeEmbedding({"budget": [0.0, 1.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=3))

    indexes = [chunk.chunk_index for chunk in results]
    assert 0 in indexes
    assert any("sparse" in chunk.channels for chunk in results)


def test_hybrid_disabled_uses_dense_only(monkeypatch):
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    embedding = FakeEmbedding({"budget": [0.0, 1.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=3))

    assert all("sparse" not in chunk.channels for chunk in results)


def test_min_score_filters_the_dense_channel(monkeypatch):
    # a2([0.9, 0.1] 归一化后与查询的余弦 ≈ 0.9939),阈值必须卡在
    # 1.0 与 0.9939 之间才能验证"只留满分命中"
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.995)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=5))

    # 只有余弦 1.0 的 a0 过线，a2(≈0.994) 与 a1(0) 被挡掉
    assert [chunk.chunk_index for chunk in results] == [0]


def test_dense_channel_failure_degrades_to_sparse():
    class BrokenEmbedding(FakeEmbedding):
        async def embed_query(self, query):
            raise RuntimeError("embedding api down")

    retriever = StubRetriever(_corpus(), embedding=BrokenEmbedding({}))

    results = run(retriever.retrieve(None, USER, "预算", top_k=3))

    assert results
    assert all(chunk.channels == ("sparse",) for chunk in results)


def test_context_window_expands_hit_with_neighbours(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CONTEXT_WINDOW", 1)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.95)
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=1))

    assert len(results) == 1
    assert results[0].chunk_range == (0, 1)
    assert "员工餐补" in results[0].content  # 相邻分块被带上了


def test_overlapping_neighbourhoods_are_merged(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CONTEXT_WINDOW", 1)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    results = run(retriever.retrieve(None, USER, "budget", top_k=3))

    # a0 的 [0,1] 与 a2 的 [1,2] 重叠，合并成一条，同一段文字不出现两遍
    assert len(results) == 1
    assert results[0].chunk_range == (0, 2)
    assert results[0].content.count("员工餐补") == 1


def test_multi_query_expands_and_records_variants(monkeypatch):
    monkeypatch.setattr(settings, "RAG_MULTI_QUERY", True)
    monkeypatch.setattr(settings, "RAG_MULTI_QUERY_COUNT", 2)
    embedding = FakeEmbedding({"预算": [1.0, 0.0], "费用审批": [0.9, 0.1]})
    adapter = StaticAdapter([json.dumps(["费用审批", "开支额度"], ensure_ascii=False)])
    retriever = StubRetriever(_corpus(), embedding=embedding, model_adapter=adapter)

    run(retriever.retrieve(None, USER, "预算", top_k=3))

    assert embedding.queries == ["预算", "费用审批", "开支额度"]


def test_multi_query_failure_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(settings, "RAG_MULTI_QUERY", True)
    embedding = FakeEmbedding({"预算": [1.0, 0.0]})
    adapter = StaticAdapter(["抱歉，我不太明白"])  # 不是 JSON 数组
    retriever = StubRetriever(_corpus(), embedding=embedding, model_adapter=adapter)

    run(retriever.retrieve(None, USER, "预算", top_k=3))

    assert embedding.queries == ["预算"]


def test_rerank_reorders_by_model_output(monkeypatch):
    monkeypatch.setattr(settings, "RAG_RERANK", True)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.0)
    embedding = FakeEmbedding({"预算": [1.0, 0.0]})
    baseline = StubRetriever(_corpus(), embedding=FakeEmbedding({"预算": [1.0, 0.0]}))
    original = [chunk.chunk_index for chunk in run(baseline.retrieve(None, USER, "预算", 3))]

    # 让模型把融合排名里的第二名顶到第一
    adapter = StaticAdapter([json.dumps([2, 1, 3])])
    retriever = StubRetriever(_corpus(), embedding=embedding, model_adapter=adapter)
    reranked = [chunk.chunk_index for chunk in run(retriever.retrieve(None, USER, "预算", 3))]

    assert original[0] != reranked[0]
    assert set(original) == set(reranked)


def test_rerank_failure_keeps_fusion_order(monkeypatch):
    monkeypatch.setattr(settings, "RAG_RERANK", True)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.0)
    embedding = FakeEmbedding({"预算": [1.0, 0.0]})
    baseline = StubRetriever(_corpus(), embedding=FakeEmbedding({"预算": [1.0, 0.0]}))
    original = [chunk.chunk_index for chunk in run(baseline.retrieve(None, USER, "预算", 3))]

    adapter = StaticAdapter(["模型今天不想排序"])
    retriever = StubRetriever(_corpus(), embedding=embedding, model_adapter=adapter)
    after = [chunk.chunk_index for chunk in run(retriever.retrieve(None, USER, "预算", 3))]

    assert after == original


def test_results_are_serialisable_for_citations():
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)

    payload = run(retriever.retrieve(None, USER, "budget", top_k=1))[0].as_dict()

    assert payload["document_name"] == "notes.md"
    assert payload["document_id"] == "d1"
    assert payload["chunk_range"] == [0, 0]
    assert payload["score"] is not None
    assert json.dumps(payload, ensure_ascii=False)


def test_format_context_carries_document_id_and_span():
    embedding = FakeEmbedding({"budget": [1.0, 0.0]})
    retriever = StubRetriever(_corpus(), embedding=embedding)
    chunks = run(retriever.retrieve(None, USER, "budget", top_k=2))

    context = format_context(chunks)

    assert "document_id: d1" in context
    assert "来源: notes.md" in context
    assert "【参考 1】" in context


def test_format_context_of_empty_result_is_empty():
    assert format_context([]) == ""


def test_results_from_multiple_documents_are_not_merged(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CONTEXT_WINDOW", 1)
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.0)
    other = _document("d2", "other.md")
    rows = _corpus() + [(_chunk("b0", 0, "另一篇文档的预算段", [1.0, 0.0], "d2"), other)]
    retriever = StubRetriever(rows, embedding=FakeEmbedding({"预算": [1.0, 0.0]}))

    results = run(retriever.retrieve(None, USER, "预算", top_k=5))

    names = {chunk.document_name for chunk in results}
    assert names == {"notes.md", "other.md"}
