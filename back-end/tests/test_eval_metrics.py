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


def test_must_avoid_strings_are_absent_from_the_corpus():
    """非注入用例的 ``must_avoid`` 不能是语料里存在的串。

    ``must_avoid`` 是对答案正文做子串匹配,所以它只能填**正确回答绝不会说出**的
    东西。语料里存在的值不满足这一条:问丧假时,一个正确回答完全可能写
    "手册只写了婚假 10 天,没有丧假规定"——那是标准答案,但如果
    ``must_avoid`` 里有 "10 天",它会被判成编造。

    我 2026-08-25 加硬负例时正是这么写错的:八条里有六条的 must_avoid 落在语料里,
    等于给正确答案埋了地雷。这条测试把那个判据钉住。

    注入用例是例外:canary 串**故意**种在语料里(它就是被注入的载荷),
    而正确回答绝不该把它复述出来。所以那两条不受这条规则约束。
    """
    import os

    from eval.runner import CORPUS_DIR

    corpus = {}
    for name in os.listdir(CORPUS_DIR):
        if name.endswith(".md"):
            with open(os.path.join(CORPUS_DIR, name), encoding="utf-8") as handle:
                corpus[name] = handle.read()

    for case in load_cases():
        if case.probe == "injection":
            continue
        for term in case.must_avoid:
            hits = [name for name, text in corpus.items() if term in text]
            assert not hits, (
                f"{case.id} 的 must_avoid={term!r} 在语料里存在（{hits}）——"
                "正确回答引用它时会被误判成编造，请换一个语料里没有的值"
            )


def test_hard_negatives_outnumber_the_trivial_ones():
    """不可回答用例要够多,而且不能全是"整个话题都不在语料里"那种。

    2026-08-25 之前只有两条(期权行权价、停车位),两个话题在语料里完全不存在,
    检索连相关分块都召不回,于是拒答很容易——测不出真实风险。真实风险是检索
    **自信地**返回一段邻近内容,而模型顺着它把缺的数字补出来。

    这条断言只守住数量下界(拒答率这一列需要足够样本:2 条时一条错就是 50% 波动)。
    "邻近性"是设计判据,没法自动断言——它写在数据集的注释与每条的
    reference_answer 里。
    """
    negatives = [case for case in load_cases() if not case.answerable]
    assert len(negatives) >= 8, (
        f"只有 {len(negatives)} 条不可回答用例，拒答率这一列的样本太少，"
        "单条波动会盖过真实差异"
    )


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


def test_zero_degradations_is_not_serialised_as_unmeasured():
    """"一次都没降级"必须是 ``0``，不能是 ``None``。

    原来是 ``sum(...) or None``，于是干净的运行和"根本没统计过"序列化成同一个值。
    报告把它渲染成 `0/54`——读起来是"测过了，很干净"。而这一列存在的全部意义就是
    分开这两件事：`0` 才能去信任同一行的其他指标，"没统计过"要先去修埋点。

    2026-08-27 那份报告里 rerank-api 有重排环节，``degradedCases`` 却和无增强的
    baseline 一样是 ``None``。（后来确认埋点是通的——同一份代码在 rerank 上记到了
    10 次——所以那个 0 是真的。但从报告上分辨不出来，这才是要修的。）
    """
    from eval.runner import summarize

    clean = summarize(VARIANTS["baseline"], [])
    assert clean["degradedCases"] == 0, "空结果集也该是 0，不是 None"
    assert clean["degradedCases"] is not None


# ========== 逐题胜负 ==========


def test_wins_and_losses_separate_two_shapes_with_the_same_mean():
    """均值相同、形状完全不同的两组，必须在胜负上分得开。

    这是 2026-08-27 rerank-api 的真实问题：均值 +0.0564 来自「18 好 / 10 差」，
    而「28 条各涨一点点」会给出同一个均值。前者上线后的表现无法预测，
    后者是稳定收益——判定列对两者都只会说「分不出来」。
    """
    volatile_base = [0.4] * 28
    volatile_variant = [0.9] * 18 + [0.0] * 10
    # 让 steady 的逐题差值恒等于 volatile 的均值，两者差值那一列就必然相同
    shared_mean = sum(
        v - b for b, v in zip(volatile_base, volatile_variant)
    ) / len(volatile_base)
    steady_base = [0.4] * 28
    steady_variant = [0.4 + shared_mean] * 28

    volatile = metrics.paired_bootstrap(volatile_base, volatile_variant)
    steady = metrics.paired_bootstrap(steady_base, steady_variant)

    assert volatile["wins"] == 18 and volatile["losses"] == 10
    assert steady["wins"] == 28 and steady["losses"] == 0
    # 均值一致（构造如此），所以差值那一列分不开这两件事
    assert abs(volatile["observed"] - steady["observed"]) < 1e-9


def test_all_equal_reports_no_wins_and_no_losses():
    """全部持平时两个计数都是 0，不是 None——「一次都没赢」是个数，不是缺数据。"""
    stats = metrics.paired_bootstrap([0.5] * 10, [0.5] * 10)
    assert stats["wins"] == 0 and stats["losses"] == 0
    assert stats["changed"] == 0


# ========== 样本量估计 ==========


def test_required_sample_size_is_larger_when_variance_is_higher():
    """同样的差值，逐题方差越大，需要的题越多。"""
    base = [0.4] * 40
    tight = [0.45] * 40  # 每条都 +0.05，零方差
    noisy = [0.9 if i % 2 else 0.0 for i in range(40)]  # 均值 +0.05，方差极大

    assert metrics.required_sample_size(base, tight) == 1, "零方差时样本量不是瓶颈"
    wanted = metrics.required_sample_size(base, noisy)
    assert wanted is not None and wanted > 40, f"高方差该要求更多题，得到 {wanted}"


def test_required_sample_size_is_none_when_there_is_no_difference():
    """差值为 0 时任何样本量都判不出方向，返回 None 而不是一个巨大的数。"""
    assert metrics.required_sample_size([0.4] * 20, [0.4] * 20) is None


def test_required_sample_size_matches_the_real_run():
    """锁住 2026-08-27 那轮的真实量级：44 条现有，precision 约需 141 条。

    数字本身不是重点，重点是**它远大于 44**——这把「分不出来」翻译成
    「题不够多」，而不是「rerank 没用」。
    """
    # 18 好 / 10 差 / 16 持平，均值 +0.0564（贴近实测形状）
    base = [0.4] * 44
    variant = [0.4] * 44
    for i in range(18):
        variant[i] = 0.4 + 0.28
    for i in range(18, 28):
        variant[i] = 0.4 - 0.17
    wanted = metrics.required_sample_size(base, variant)
    assert wanted is not None and wanted > 44, (
        f"实测形状下应当要求远多于 44 条，得到 {wanted}"
    )


# ========== precision 变化的成分 ==========


def test_same_document_set_is_counted_as_an_artifact():
    """两侧取回同一批文档、只有分块数不同 → 算假象，不算真实变化。

    ``postmortem-p2-trigger`` 就是这个形状：1 个分块 → 2 个分块，
    两侧都只取回正确文档，precision 掉 0.500 而 nDCG 两边都是 1.000。
    """
    parts = metrics.decompose_precision_change(
        [(["a.md"], ["a.md", "a.md"], ["a.md"], -0.5)]
    )
    assert parts["artifactCases"] == 1
    assert parts["realImproved"] == 0 and parts["realRegressed"] == 0
    assert parts["artifactMean"] == -0.5


def test_different_document_set_is_counted_as_real():
    """取回的文档集合真的变了 → 算真实变化。"""
    parts = metrics.decompose_precision_change(
        [(["a.md", "b.md"], ["a.md"], ["a.md"], +0.5)]
    )
    assert parts["realImproved"] == 1 and parts["artifactCases"] == 0
    assert parts["realMean"] == 0.5


def test_unchanged_cases_are_excluded_from_both_counts():
    """差值为 0 的题两边都不进：它既不是真实变化也不是假象。"""
    parts = metrics.decompose_precision_change(
        [(["a.md"], ["a.md"], ["a.md"], 0.0)] * 5
    )
    assert parts["artifactCases"] == 0
    assert parts["realImproved"] == 0 and parts["realRegressed"] == 0


def test_decomposition_means_are_spread_over_all_pairs():
    """两个均值摊在**全部**配对题上，好和主表那个差值直接对得上。

    只摊在"有变化的题"上的话，两个分量加起来会大于主表的差值，
    读者没法把它们对起来。
    """
    pairs = [
        (["a.md", "b.md"], ["a.md"], ["a.md"], +0.4),  # 真实
        (["a.md"], ["a.md", "a.md"], ["a.md"], -0.2),  # 假象
        (["a.md"], ["a.md"], ["a.md"], 0.0),  # 持平
        (["a.md"], ["a.md"], ["a.md"], 0.0),
    ]
    parts = metrics.decompose_precision_change(pairs)
    total = parts["realMean"] + parts["artifactMean"]
    assert abs(total - (0.4 - 0.2) / 4) < 1e-12, "两个分量之和该等于总差值"
