"""eval/metrics.py 里分位与校准统计的纯函数测试（不触模型、不触库）。

覆盖 B1 的 percentile 与 B2 的一致性统计（agreement / MAE / 加权 kappa / spearman）——
它们是裁判校准报告与 P95 延迟的计算内核，用手算得出的已知值钉住。
"""
from __future__ import annotations

import pytest

from eval import metrics


def test_percentile_interpolates_and_none_on_empty():
    assert metrics.percentile([], 95) is None
    assert metrics.percentile([5.0], 95) == 5.0
    assert metrics.percentile([0.0, 1.0], 50) == pytest.approx(0.5)
    assert metrics.percentile([0.0, 0.5, 1.0], 100) == pytest.approx(1.0)
    assert metrics.percentile([10, 20, 30, 40], 95) == pytest.approx(38.5)
    assert metrics.percentile([1, 2, 3, 4, 5], 50) == pytest.approx(3.0)


def test_agreement_rate():
    assert metrics.agreement_rate([1, 2, 3], [1, 2, 3]) == 1.0
    assert metrics.agreement_rate([1, 2, 3], [1, 2, 4]) == pytest.approx(2 / 3)
    assert metrics.agreement_rate([], []) is None
    assert metrics.agreement_rate([1], [1, 2]) is None


def test_mean_absolute_error():
    assert metrics.mean_absolute_error([1, 2, 3], [1, 2, 3]) == 0.0
    assert metrics.mean_absolute_error([1, 2, 3], [2, 2, 5]) == pytest.approx(1.0)
    assert metrics.mean_absolute_error([], []) is None


def test_cohens_kappa_perfect_and_degenerate():
    assert metrics.cohens_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
    # 全落同一档：没有可分辨的变化，kappa 无定义（不是 1.0）
    assert metrics.cohens_kappa([3, 3, 3], [3, 3, 3]) is None
    assert metrics.cohens_kappa([], []) is None


def test_cohens_kappa_total_disagreement_is_negative():
    # 二分类完全反着标：kappa = -1
    assert metrics.cohens_kappa(
        [0, 0, 1, 1], [1, 1, 0, 0], weighted=False
    ) == pytest.approx(-1.0)


def test_cohens_kappa_weighted_is_more_lenient_on_near_miss():
    # 只差 1 分的序数分歧：加权（差得少扣得少）应比无权（一律算错）宽容
    human = [1, 2, 3, 4, 5]
    judge = [1, 2, 3, 4, 4]
    weighted = metrics.cohens_kappa(human, judge, weighted=True)
    unweighted = metrics.cohens_kappa(human, judge, weighted=False)
    assert weighted is not None and unweighted is not None
    assert weighted > unweighted


def test_spearman_monotonic_and_ties():
    assert metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert metrics.spearman([1], [1]) is None
    # 带并列：平均秩下仍是完全相关
    assert metrics.spearman([1, 1, 2], [5, 5, 9]) == pytest.approx(1.0)
