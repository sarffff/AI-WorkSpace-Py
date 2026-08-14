"""语义缓存的行为测试。

缓存最危险的地方不是"没命中"，而是"该不命中的时候命中了"。所以这里大部分用例
都在验证**不命中**：换了模型、关了 RAG、超过 TTL、知识库变了、相似度不够。
"""
from __future__ import annotations

from conftest import run
from config import settings
from services import semantic_cache as cache_module
from services.semantic_cache import SemanticCache, _cosine, _normalize


class FakeEmbedder:
    """把问题映射成预设向量；没登记过的问题给一个正交向量。"""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.calls: list[str] = []
        self.fails = False

    async def embed_query(self, query: str) -> list[float]:
        self.calls.append(query)
        if self.fails:
            raise RuntimeError("embedding down")
        return self.vectors.get(query, [0.0, 0.0, 1.0])


def make_cache(
    monkeypatch, vectors: dict[str, list[float]] | None = None
) -> tuple[SemanticCache, FakeEmbedder]:
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_THRESHOLD", 0.95)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 200)
    cache = SemanticCache()
    embedder = FakeEmbedder(vectors)
    cache._embedder = embedder
    return cache, embedder


def test_disabled_cache_never_hits(monkeypatch):
    cache, _embedder = make_cache(monkeypatch)
    run(cache.store("u1", "问题", "答案", "glm", False))
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLED", False)

    assert run(cache.lookup("u1", "问题", "glm", False)) is None


def test_exact_match_hits_without_embedding_call(monkeypatch):
    """原样再问一遍是最常见的情况，这条路不该花一次 embedding 调用。"""
    cache, embedder = make_cache(monkeypatch)
    run(cache.store("u1", "试用期多久？", "6 个月", "glm", False))
    embedder.calls.clear()

    hit = run(cache.lookup("u1", "  试用期多久 ", "glm", False))

    assert hit is not None
    assert hit.exact and hit.similarity == 1.0
    assert embedder.calls == [], "精确匹配不该触发向量化"


def test_other_user_never_hits(monkeypatch):
    """跨用户命中是数据泄露，不是优化。"""
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "q", "答案", "glm", False))

    assert run(cache.lookup("u2", "q", "glm", False)) is None


def test_different_model_never_hits(monkeypatch):
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "q", "答案", "glm-4.5-air", False))

    assert run(cache.lookup("u1", "q", "glm-4.6", False)) is None


def test_rag_flag_change_never_hits(monkeypatch):
    """关掉 RAG 后答案的依据都变了，不该复用。"""
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "q", "答案", "glm", True))

    assert run(cache.lookup("u1", "q", "glm", False)) is None


def test_prompt_version_change_never_hits_exactly(monkeypatch):
    """换了提示词版本就是换了实验组，命中旧答案等于 A/B 白做。"""
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(
        cache.store(
            "u1", "q", "答案", "glm", False, prompt_ref="chat_system_rag@v2"
        )
    )

    assert (
        run(cache.lookup("u1", "q", "glm", False, prompt_ref="chat_system_rag@v3-lean"))
        is None
    )
    hit = run(
        cache.lookup("u1", "q", "glm", False, prompt_ref="chat_system_rag@v2")
    )
    assert hit is not None and hit.exact


def test_prompt_version_change_never_hits_semantically(monkeypatch):
    """语义通道也要过滤版本，否则精确匹配挡住了、相似匹配又放进来。"""
    cache, _embedder = make_cache(
        monkeypatch,
        {"试用期多久": [1.0, 0.0, 0.0], "试用期是几个月": [0.99, 0.14, 0.0]},
    )
    run(
        cache.store(
            "u1", "试用期多久", "6 个月", "glm", False, prompt_ref="chat_system_rag@v2"
        )
    )

    assert (
        run(
            cache.lookup(
                "u1", "试用期是几个月", "glm", False, prompt_ref="chat_system_rag@v1"
            )
        )
        is None
    )


def test_similar_question_hits_above_threshold(monkeypatch):
    cache, _embedder = make_cache(
        monkeypatch,
        {"试用期多久": [1.0, 0.0, 0.0], "试用期是几个月": [0.99, 0.14, 0.0]},
    )
    run(cache.store("u1", "试用期多久", "6 个月", "glm", False))

    hit = run(cache.lookup("u1", "试用期是几个月", "glm", False))

    assert hit is not None and not hit.exact
    assert hit.entry.answer == "6 个月"


def test_dissimilar_question_misses(monkeypatch):
    cache, _embedder = make_cache(
        monkeypatch, {"报销上限": [1.0, 0.0, 0.0], "年假几天": [0.0, 1.0, 0.0]}
    )
    run(cache.store("u1", "报销上限", "5000 元", "glm", False))

    assert run(cache.lookup("u1", "年假几天", "glm", False)) is None


def test_threshold_is_respected(monkeypatch):
    cache, _embedder = make_cache(
        monkeypatch, {"a": [1.0, 0.0, 0.0], "b": [0.9, 0.436, 0.0]}
    )
    run(cache.store("u1", "a", "答案", "glm", False))

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_THRESHOLD", 0.99)
    assert run(cache.lookup("u1", "b", "glm", False)) is None

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_THRESHOLD", 0.85)
    assert run(cache.lookup("u1", "b", "glm", False)) is not None


def test_expired_entry_is_pruned(monkeypatch):
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "q", "答案", "glm", False))
    # 把这条的时间戳推到 TTL 之外
    cache._store.bucket("u1")[0].created_at = 0.0

    assert run(cache.lookup("u1", "q", "glm", False)) is None


def test_invalidate_user_clears_bucket(monkeypatch):
    cache, _embedder = make_cache(monkeypatch, {"q": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "q", "答案", "glm", False))

    assert cache.invalidate_user("u1") == 1
    assert run(cache.lookup("u1", "q", "glm", False)) is None


def test_embedding_failure_degrades_to_miss(monkeypatch):
    """向量化挂了要当未命中，不能让缓存故障拖垮回答。"""
    cache, embedder = make_cache(monkeypatch, {"a": [1.0, 0.0, 0.0]})
    run(cache.store("u1", "a", "答案", "glm", False))
    embedder.fails = True

    assert run(cache.lookup("u1", "b", "glm", False)) is None


def test_store_survives_embedding_failure(monkeypatch):
    """写入时向量化失败仍要留下这条，至少精确匹配还能用。"""
    cache, embedder = make_cache(monkeypatch)
    embedder.fails = True
    run(cache.store("u1", "q", "答案", "glm", False))
    embedder.fails = False

    hit = run(cache.lookup("u1", "q", "glm", False))
    assert hit is not None and hit.exact


def test_empty_answer_is_not_cached(monkeypatch):
    cache, _embedder = make_cache(monkeypatch)
    run(cache.store("u1", "q", "   ", "glm", False))

    assert cache._store.bucket("u1") == []


def test_max_entries_drops_oldest(monkeypatch):
    cache, _embedder = make_cache(monkeypatch)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 2)
    for index in range(4):
        run(cache.store("u1", f"q{index}", f"a{index}", "glm", False))

    questions = [entry.question for entry in cache._store.bucket("u1")]
    assert questions == ["q2", "q3"]


def test_hit_rate_is_none_before_any_lookup(monkeypatch):
    cache, _embedder = make_cache(monkeypatch)

    # 没查过 ≠ 0% 命中率
    assert cache.stats()["hitRate"] is None


def test_tokens_saved_accumulates_on_hit(monkeypatch):
    cache, _embedder = make_cache(monkeypatch)
    run(
        cache.store(
            "u1", "q", "答案", "glm", False, prompt_tokens=100, completion_tokens=50
        )
    )

    run(cache.lookup("u1", "q", "glm", False))

    assert cache.stats()["tokensSaved"] == 150
    assert cache.stats()["hits"] == 1


def test_normalize_folds_whitespace_and_punctuation():
    assert _normalize("  试用期  多久？ ") == "试用期 多久"
    assert _normalize("Hello World!") == "hello world"


def test_cosine_of_mismatched_vectors_is_zero():
    assert _cosine([1.0], [1.0, 0.0]) == 0.0
    assert _cosine([], []) == 0.0
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_module_singleton_exists():
    assert cache_module.semantic_cache is not None
