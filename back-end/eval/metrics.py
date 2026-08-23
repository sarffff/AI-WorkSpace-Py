"""检索指标。

全部是纯函数、不调模型，所以跑起来是零成本且完全确定的——这也是为什么
检索指标应该先看、答案质量的 LLM 评分后看：便宜的信号先用尽。

关键约定：
1. **相关性标在文档级，不是分块级**。分块配置一改，chunk id 全变，
   标在分块上的金标准立刻作废；标在文档上则任何分块/检索配置都能复用
   同一批标注。
2. **@k 按原始返回位置截断，去重只用于计数**。若先按文档去重再截断，
   "同一文档的重复分块挤占 top-k"这一失效模式会被指标掩盖——而它
   恰恰是这套系统明确关心的问题(文档级去重功能就是为它做的)。
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
    """前 k 个**位置**覆盖了多少比例的相关文档。

    同一文档命中多个分块只算一次覆盖,但 k 数的是原始位置:前 2 位被同一
    文档的两个分块占掉时,第二个位置没有带来新覆盖,recall 就是砍半的。
    """
    if not relevant:
        return 1.0
    covered = {item for item in ranked[:k] if item in relevant}
    return len(covered) / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个位置里,相关文档的覆盖比例(每文档只计一次)。"""
    top = ranked[:k]
    if not top:
        return 0.0
    hits = {item for item in top if item in relevant}
    return len(hits) / len(top)


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
    同一文档的重复命中只在首次出现的位置计一次增益。
    """
    if not relevant:
        return 1.0
    seen: set[str] = set()
    dcg = 0.0
    for position, item in enumerate(ranked[:k], start=1):
        if item in relevant and item not in seen:
            seen.add(item)
            dcg += 1.0 / log2(position + 1)
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


def _fold(text: str) -> str:
    """小写 + 去掉全部空白，用于关键词比对。

    去空白是 2026-08-23 加的，它解决的是一个把数据集逼进死角的问题：

    原来是纯子串匹配，于是 ``"3 个工作日"`` 匹配不上模型写的 ``"3个工作日"``——
    中文数字与量词之间加不加空格纯属排版习惯。为了绕开它，金标只能退化成断言
    **裸数字** ``"3"``，而裸数字会被同一篇文档里更长的数字命中：``"5"`` 落在
    ``"15 天"`` 里、``"30"`` 落在 ``"300 元"`` 里、``"6"`` 落在 ``"2026 年"`` 里。
    后果是**答错也算满分**——问的是结转 5 天，模型答陪产假 15 天，覆盖率仍然 1.0。
    30 条老题里有 9 条带这个缺陷。

    归一化之后 ``"5 天"`` / ``"30%"`` / ``"6 个月"`` 这类"数字+量词"重新可用：
    它们既不怕排版差异，也不会被更长的数字整体包含。
    """
    return "".join(text.lower().split())


def keyword_coverage(answer: str, keywords: list[str]) -> float:
    """答案里出现了多少比例的关键事实。

    这是个粗糙但零成本的信号：命中率低几乎肯定有问题，命中率高也不代表答案对
    （数字可能出现在否定句里）。所以它只用来筛出明显失败的样本，
    最终判定交给 LLM-as-judge。

    比对前两侧都做 ``_fold``（小写 + 去空白），原因见那里。
    """
    if not keywords:
        return 1.0
    folded = _fold(answer)
    return sum(1 for keyword in keywords if _fold(keyword) in folded) / len(keywords)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
