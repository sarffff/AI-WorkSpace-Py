"""检索指标。

全部是纯函数、不调模型，所以跑起来是零成本且完全确定的——这也是为什么
检索指标应该先看、答案质量的 LLM 评分后看：便宜的信号先用尽。

关键约定：**相关性标在文档级，不是分块级**。分块配置一改，chunk id 全变，
标在分块上的金标准立刻作废；标在文档上则任何分块/检索配置都能复用同一批标注。
"""
from __future__ import annotations

from math import log2


def _first_occurrence(ranked: list[str]) -> list[str]:
    """按首次出现去重，保持排序。检索结果里同一文档常有多个分块命中。"""
    seen: set[str] = set()
    unique: list[str] = []
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个结果覆盖了多少比例的相关文档。没有相关文档时返回 1.0（无从漏召）。"""
    if not relevant:
        return 1.0
    top = set(_first_occurrence(ranked)[:k])
    return len(top & relevant) / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个结果里有多少比例是相关的。"""
    top = _first_occurrence(ranked)[:k]
    if not top:
        return 0.0
    return sum(1 for item in top if item in relevant) / len(top)


def mrr(ranked: list[str], relevant: set[str]) -> float:
    """首个相关结果排名的倒数。只关心「第一个对的东西排多前」。"""
    if not relevant:
        return 1.0
    for position, item in enumerate(_first_occurrence(ranked), start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """二值增益的 nDCG@k。

    相比 recall，它对「相关结果排在第 1 位还是第 5 位」敏感——重排是否有效
    主要看这个指标，因为重排不改变召回集合，只改变顺序。
    """
    if not relevant:
        return 1.0
    top = _first_occurrence(ranked)[:k]
    dcg = sum(
        1.0 / log2(position + 1)
        for position, item in enumerate(top, start=1)
        if item in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / log2(position + 1) for position in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def evaluate_ranking(
    ranked: list[str], relevant: set[str], k: int = 5
) -> dict[str, float]:
    return {
        f"recall@{k}": recall_at_k(ranked, relevant, k),
        f"precision@{k}": precision_at_k(ranked, relevant, k),
        f"ndcg@{k}": ndcg_at_k(ranked, relevant, k),
        "mrr": mrr(ranked, relevant),
    }


def keyword_coverage(answer: str, keywords: list[str]) -> float:
    """答案里出现了多少比例的关键事实。

    这是个粗糙但零成本的信号：命中率低几乎肯定有问题，命中率高也不代表答案对
    （数字可能出现在否定句里）。所以它只用来筛出明显失败的样本，
    最终判定交给 LLM-as-judge。
    """
    if not keywords:
        return 1.0
    lowered = answer.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered) / len(keywords)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
