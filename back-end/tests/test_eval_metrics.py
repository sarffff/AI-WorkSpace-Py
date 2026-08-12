"""检索指标与金标准集的自检。

指标本身是纯函数，值得逐个钉死——它们是后面所有配置决策的度量基准，
量错了会一路错下去。
"""
from __future__ import annotations

import pytest

from eval import metrics
from eval.runner import load_cases
from eval.variants import VARIANTS, resolve


def test_recall_counts_documents_not_chunks():
    """同一文档命中多个分块只算一次覆盖。"""
    ranked = ["a.md", "a.md", "a.md", "b.md"]
    assert metrics.recall_at_k(ranked, {"a.md", "b.md"}, 2) == 0.5
    assert metrics.recall_at_k(ranked, {"a.md", "b.md"}, 4) == 1.0


def test_precision_dedupes_before_cutting():
    ranked = ["a.md", "a.md", "b.md"]
    # 去重后前 2 名是 a.md、b.md，其中 1 个相关
    assert metrics.precision_at_k(ranked, {"a.md"}, 2) == 0.5


def test_precision_of_empty_result_is_zero():
    assert metrics.precision_at_k([], {"a.md"}, 5) == 0.0


def test_mrr_uses_first_relevant_position():
    assert metrics.mrr(["x.md", "y.md", "a.md"], {"a.md"}) == pytest.approx(1 / 3)
    assert metrics.mrr(["a.md"], {"a.md"}) == 1.0
    assert metrics.mrr(["x.md"], {"a.md"}) == 0.0


def test_ndcg_rewards_higher_placement():
    """recall 相同、排序不同时 nDCG 必须能区分——这是判断重排有效的依据。"""
    top = metrics.ndcg_at_k(["a.md", "x.md", "y.md"], {"a.md"}, 3)
    bottom = metrics.ndcg_at_k(["x.md", "y.md", "a.md"], {"a.md"}, 3)
    assert top == 1.0
    assert bottom < top
    assert metrics.recall_at_k(["a.md", "x.md", "y.md"], {"a.md"}, 3) == metrics.recall_at_k(
        ["x.md", "y.md", "a.md"], {"a.md"}, 3
    )


def test_ndcg_is_one_when_all_relevant_are_on_top():
    assert metrics.ndcg_at_k(["a.md", "b.md", "x.md"], {"a.md", "b.md"}, 3) == 1.0


def test_no_relevant_documents_scores_perfect():
    """absent 类问题没有相关文档，检索指标不该把它算成失败。"""
    assert metrics.recall_at_k(["x.md"], set(), 5) == 1.0
    assert metrics.ndcg_at_k(["x.md"], set(), 5) == 1.0
    assert metrics.mrr(["x.md"], set()) == 1.0


def test_keyword_coverage_is_case_insensitive():
    assert metrics.keyword_coverage("值为 HMAC-SHA256", ["hmac"]) == 1.0
    assert metrics.keyword_coverage("只有一个 429", ["429", "限流"]) == 0.5
    assert metrics.keyword_coverage("任意内容", []) == 1.0


def test_mean_of_empty_is_none():
    assert metrics.mean([]) is None
    assert metrics.mean([1.0, 2.0]) == 1.5


def test_evaluate_ranking_keys_include_k():
    scores = metrics.evaluate_ranking(["a.md"], {"a.md"}, k=3)
    assert set(scores) == {"recall@3", "precision@3", "ndcg@3", "mrr"}


# ---- 金标准集自检：数据集本身写错了，后面所有数字都没意义 ----


def test_dataset_ids_are_unique():
    cases = load_cases()
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))


def test_dataset_covers_every_probe_type():
    probes = {case.probe for case in load_cases()}
    assert {"lexical", "paraphrase", "table_lookup", "absent"} <= probes


def test_answerable_cases_have_expected_documents():
    for case in load_cases():
        if case.answerable:
            assert case.expected_documents, f"{case.id} 缺少来源标注"
        else:
            assert not case.expected_documents, f"{case.id} 不该有来源标注"


def test_expected_documents_exist_in_corpus():
    import os

    from eval.runner import CORPUS_DIR

    available = {name for name in os.listdir(CORPUS_DIR) if name.endswith(".md")}
    for case in load_cases():
        for name in case.expected_documents:
            assert name in available, f"{case.id} 引用了不存在的文档 {name}"


def test_limit_truncates_dataset():
    assert len(load_cases(limit=3)) == 3


def test_resolve_defaults_to_baseline():
    assert [variant.name for variant in resolve(None)] == ["baseline"]
    assert [variant.name for variant in resolve([])] == ["baseline"]


def test_resolve_all_returns_every_variant():
    assert len(resolve(["all"])) == len(VARIANTS)


def test_resolve_rejects_unknown_variant():
    with pytest.raises(SystemExit):
        resolve(["nope"])


def test_every_variant_pins_the_swept_switches():
    """变体必须写全关键开关，否则结果会受本地 .env 影响，跨机器没法比。"""
    required = {"RAG_HYBRID", "RAG_MULTI_QUERY", "RAG_RERANK", "RAG_TOP_K"}
    for variant in VARIANTS.values():
        assert required <= set(variant.overrides), variant.name
