"""多次运行方差：metrics.dispersion + run.aggregate_repeats + render_variance（纯函数，无模型）。"""
from __future__ import annotations

from eval import metrics
from eval.run import aggregate_repeats, render_variance


def test_dispersion_basic():
    disp = metrics.dispersion([0.8, 0.6, 0.7])
    assert disp["n"] == 3
    assert abs(disp["mean"] - 0.7) < 1e-9
    assert disp["min"] == 0.6 and disp["max"] == 0.8
    assert disp["stdev"] > 0 and disp["cv"] > 0


def test_dispersion_single_value_no_spread():
    disp = metrics.dispersion([0.5])
    assert disp["n"] == 1 and disp["stdev"] == 0.0 and disp["cv"] == 0.0


def test_dispersion_empty_is_none():
    assert metrics.dispersion([]) is None


def test_dispersion_zero_mean_cv_none():
    """全 0 → 均值 0 → cv 无意义给 None（不是 0，也不除零崩）。"""
    disp = metrics.dispersion([0.0, 0.0])
    assert disp["mean"] == 0.0 and disp["cv"] is None


def _report(faith, rel, prec):
    return {"summaries": [{"variant": "baseline", "faithfulness": faith,
                           "relevance": rel, "precision@5": prec}]}


def test_aggregate_repeats_groups_by_variant_metric():
    reports = [_report(0.8, 0.9, 0.4), _report(0.6, 0.9, 0.5)]
    agg = aggregate_repeats(reports)
    assert set(agg) == {"baseline"}
    faith = agg["baseline"]["faithfulness"]
    assert faith["n"] == 2 and abs(faith["mean"] - 0.7) < 1e-9
    # relevance 两次相同 → stdev 0
    assert agg["baseline"]["relevance"]["stdev"] == 0.0
    # precision@5 经 _pick 的前缀匹配也进来了
    assert agg["baseline"]["precision"]["n"] == 2


def test_render_variance_has_cv_and_variant():
    variance = {"repeat": 3, "temperature": 0.7,
                "byVariant": {"baseline": {"faithfulness": metrics.dispersion([0.8, 0.6, 0.7])}}}
    text = "\n".join(render_variance(variance))
    assert "多次运行方差" in text and "temperature=0.7" in text
    assert "cv" in text and "baseline" in text
