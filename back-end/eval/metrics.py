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

import random
from math import log2
from statistics import stdev


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


def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数。空列表返回 None（与 mean 同约定：报告里显示"未知"而非 0）。

    均值会被一两条极端样本带偏，而"一半样本多快 / 最慢的 5% 多慢"才是延迟的实际
    形状——standard.md 的分维度指标明确点名 P95。

    与 services/chunking._percentile 同一套插值，但那边空列表返 inf（拿去比阈值），
    这里返 None（进报告聚合，和 mean 的 None 对齐）。不跨层 import，两处各留一份。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(100.0, p)) / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def dispersion(values: list[float]) -> dict[str, float | int | None] | None:
    """同一指标多次重跑的离散度。空列表给 None（同 percentile/mean 的"未知"约定）。

    为什么要它：eval 跑在温度 0 上，重跑结果几乎逐位相同；而线上是温度 0.7，
    "真实表现会更抖"（standard.md）。拿同一套题在温度 >0 上重跑 K 次、看这里的
    stdev / cv，才量得出那个抖动有多大——一个均值 0.80 但 cv 0.3 的变体，上线后
    任意一次回答都可能明显更差，这是单跑一次的点估计永远看不到的。

    cv（变异系数 = stdev/mean）是跨指标可比的那个数：忠实度和延迟量纲不同，
    stdev 不能直接比大小，cv 归一化之后能。mean 为 0 时 cv 无意义，给 None。
    """
    if not values:
        return None
    mean = sum(values) / len(values)
    spread = stdev(values) if len(values) > 1 else 0.0
    return {
        "mean": mean,
        "stdev": spread,
        "min": min(values),
        "max": max(values),
        "cv": (spread / mean) if mean else None,
        "n": len(values),
    }


def paired_bootstrap(
    baseline: list[float],
    variant: list[float],
    *,
    resamples: int = 20000,
    seed: int = 20260826,
) -> dict[str, float | int | bool] | None:
    """配对自助法:变体与 baseline 的差值,带 95% 置信区间。

    ## 为什么需要它

    2026-08-24 那份报告里 baseline 与 format-docx 的 nDCG 是 0.974 vs 0.971。
    三位小数、不带任何不确定性,读起来像"降级让排序略微变差"。离线重算之后:
    **44 条里只有 1 条的 nDCG 不同**,recall 与 precision 全部逐位相同。整个
    0.003 的差距来自一条题。同一份报告里拒答率 0.500 → 1.000 也是 1/2 变 2/2。

    所以这一层解决的不是"算得准不准",而是"这个差值配不配得出结论"。

    ## 为什么配对,而不是各自算区间

    两个变体跑的是**同一批题**,所以逐题相减能把"这道题本身难不难"整个消掉。
    独立比较两个均值会把题目难度的方差算进噪声里,功效低得多——而这套 eval
    只有 44 条计分题,功效是稀缺资源。

    ## 为什么自助法,而不是 t 检验

    ``precision@5`` 的取值是离散的(0、0.2、0.4…),``recall@5`` 更是几乎恒为 1.0。
    n=44 时正态性假设站不住,而自助法不需要它:直接对逐题差值有放回重采样,
    看均值的分布长什么样。

    ``seed`` 固定,所以同一份 JSON 每次渲染出同一个区间——报告之间可以对照,
    不会因为随机数变化而看起来"结论变了"。

    返回 ``None`` 表示配不上对(空列表或长度不等)。``detectable`` 是当前样本量下
    能分辨的最小差值:实测差值小于它时,结论是"题不够多",不是"没有差别"。
    """
    if not baseline or len(baseline) != len(variant):
        return None
    diffs = [v - b for b, v in zip(baseline, variant)]
    n = len(diffs)
    observed = sum(diffs) / n

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    low = means[int(0.025 * resamples)]
    high = means[min(int(0.975 * resamples), resamples - 1)]

    # ``detectable`` 用正态近似(1.96σ/√n)算,而判定用的是自助法分位数——两者
    # 在偏斜分布上会**互相矛盾**:2026-08-27 那轮 rerank 的 precision 差值
    # +0.0424 小于阈值 ±0.0472,却被判成真实差异,因为区间 [+0.0008, +0.0947]
    # 确实不跨 0。既然选自助法的理由就是不假设正态,阈值就不该回头用正态算。
    #
    # 所以它不再进报告,只留在 JSON 里当粗略参考。报告改印 ``stability``:
    # 换种子重跑,判定还成不成立。那个问题才是读者真正要问的。
    spread = stdev(diffs) if n > 1 else 0.0
    return {
        "n": n,
        "observed": observed,
        "low": low,
        "high": high,
        # 区间不跨 0 才算方向可信。跨 0 的正确读法是"这份数据分不出来",
        # 既不是"两者相同",也不是"变体没用"。
        "significant": not (low <= 0.0 <= high),
        "changed": sum(1 for d in diffs if abs(d) > 1e-12),
        # 逐题胜负。**均值对这类变体是错的摘要**：2026-08-27 的 rerank-api 是
        # 18 好 / 10 差、均值 +0.0564，和"28 条各涨一点点"在均值上长得一样,
        # 但前者上线后的表现完全不可预测,后者是稳定收益。判定那一列只会说
        # "分不出来",说不出是哪一种。
        "wins": sum(1 for d in diffs if d > 1e-12),
        "losses": sum(1 for d in diffs if d < -1e-12),
        "detectable": 1.96 * spread / (n**0.5) if n > 1 else 0.0,
    }


def required_sample_size(
    baseline: list[float], variant: list[float], *, power: float = 0.8
) -> int | None:
    """要判定当前这个差值，金标集得有多少条题。

    ## 为什么这个数比「分不出来」有用

    2026-08-27 的 rerank-api：差值 ``+0.0564``、区间 ``[-0.0102, +0.1299]``、
    判定「分不出来」。读到这里读者只知道"不确定"，不知道**该怎么办**——是这个
    技术没用，还是题不够多？

    这个函数回答后者：按当前观察到的效应量与逐题方差，要 80% 的把握检出它，
    需要多少条配对题。数出来远大于现有题数，说明结论是"题不够"而不是"没效果"；
    数出来接近现有题数，说明真的没有稳定效应。

    公式是配对 t 检验的样本量估计 ``n = ((z_α + z_β) · σ / δ)²``，
    σ 用逐题差值的标准差，δ 用观察到的均值差。返回 ``None`` 表示算不出
    （差值为 0，或只有一条题）——差值为 0 时任何样本量都判不出方向。

    这只是个量级估计，不是承诺：它假设加进来的新题与现有题同分布，而实际扩容
    往往会改变分布（[[corpus-expansion-wave2]] 那次就没解开 recall 饱和）。
    """
    if len(baseline) != len(variant) or len(baseline) < 2:
        return None
    diffs = [v - b for b, v in zip(baseline, variant)]
    delta = sum(diffs) / len(diffs)
    if abs(delta) < 1e-12:
        return None
    spread = stdev(diffs)
    if spread < 1e-12:
        # 逐题差值毫无方差：一条题就能定方向，样本量不是瓶颈
        return 1
    z_alpha = 1.96  # 双侧 0.05
    z_beta = 0.84 if power >= 0.8 else 0.0
    return int(((z_alpha + z_beta) * spread / abs(delta)) ** 2) + 1


def decompose_precision_change(
    pairs: list[tuple[list[str], list[str], list[str], float]],
) -> dict[str, float | int]:
    """把 precision 的变化拆成「真的变了」和「只是分块数变了」。

    ## 为什么需要拆

    ``precision@k`` 是按**文档**去重、按**位置**计数的（理由见 ``precision_at_k``）。
    于是两侧取回的文档集合完全相同时，光是分块数不同就能让它变动——而那不是
    检索变好或变差。

    2026-08-27 那份报告里最难看的一条 ``postmortem-p2-trigger`` 是 ``-0.500``：
    baseline 取回 1 个分块、rerank-api 取回 2 个分块，**两侧都只取回了正确文档**，
    nDCG 两边都是 1.000。这个指标在惩罚"把对的文档多取回来一点"。

    拆开之后那一轮是：真实变化 ``+0.0545``、纯假象 ``+0.0019``——96% 是真的。
    但不拆的话，读者要么被那条 ``-0.500`` 带偏，要么无从判断整个 ``+0.0564``
    有多少可信。

    每个元素是 ``(baseline 取回, 变体取回, 期望来源, precision 差值)``。
    判据是**文档集合是否相同**：相同就只可能是分块数造成的。
    """
    artifact = real_up = real_down = 0
    artifact_sum = real_sum = 0.0
    for base_docs, variant_docs, expected, delta in pairs:
        if abs(delta) < 1e-12:
            continue
        if set(base_docs) == set(variant_docs):
            artifact += 1
            artifact_sum += delta
        else:
            real_sum += delta
            if delta > 0:
                real_up += 1
            else:
                real_down += 1
    total = len(pairs)
    return {
        "artifactCases": artifact,
        "realImproved": real_up,
        "realRegressed": real_down,
        # 摊到全部配对题上，好和主表的均值差直接对得上
        "artifactMean": artifact_sum / total if total else 0.0,
        "realMean": real_sum / total if total else 0.0,
    }


def verdict_stability(
    baseline: list[float],
    variant: list[float],
    *,
    seeds: int = 20,
    resamples: int = 2000,
) -> float | None:
    """换种子重跑，判定还成立的比例。

    自助法的区间依赖随机数。固定种子让报告可复现，但也藏起一个问题：**这个判定
    是不是刚好卡在种子上**。差值贴着 0 的时候（2026-08-27 那轮 rerank 的
    precision 下界只有 +0.0008），这是读者第一个该问的问题。

    重采样次数比主估计少一个量级：这是稳健性探针，不是要给出更精确的区间，
    而 20 个种子 × 20000 次会让渲染明显变慢。

    返回 1.0 = 每个种子都判显著；0.0 = 每个种子都判不显著。两者都是好消息——
    它们说明结论不来自随机数。**中间值才是警告**：0.6 意味着这条结论有四成
    时候会反过来，此时该做的是加题，不是挑一个好看的种子。
    """
    if not baseline or len(baseline) != len(variant):
        return None
    hits = 0
    for index in range(seeds):
        stats = paired_bootstrap(
            baseline, variant, resamples=resamples, seed=index * 7919 + 1
        )
        if stats and stats["significant"]:
            hits += 1
    return hits / seeds


# ---- 裁判校准用的一致性统计 ----
# 拿裁判分与人工标注比对的纯函数（编排见 eval/calibration.py）。放这儿而不是
# calibration.py：它们和上面的 paired_bootstrap 一样是可单测的纯统计，
# 而"跑裁判、组织报告"那部分才留在 calibration.py。


def agreement_rate(a: list[float], b: list[float]) -> float | None:
    """两组标注逐条完全相等的比例。配不上对（空或长度不等）返回 None。"""
    if not a or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def mean_absolute_error(a: list[float], b: list[float]) -> float | None:
    """逐条绝对差的均值。序数分数（1-5）上比"完全相等"更细：差 1 分和差 4 分不是
    一回事，而 agreement_rate 把它们都算成"不一致"。"""
    if not a or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

def cohens_kappa(a: list[int], b: list[int], *, weighted: bool = True) -> float | None:
    """Cohen's kappa：扣掉"瞎猜也能撞上"的一致性后，还剩多少真一致。

    为什么不看纯一致率：两个都爱打 4 分的标注者光靠打分分布就能撞上一大片，
    一致率虚高。kappa 拿边际分布估出"偶然一致"并扣掉——这才是 standard.md 说
    "judge 要用人工标注校准"该给的数，而非被基线率灌水的百分比。

    ``weighted`` 用二次加权（序数分数适用）：差 1 分算轻微、差 4 分算严重，
    而不是无权版那样"只要不完全相等就一律算错"。所有标注落在同一档时 kappa
    无定义，返回 None（"没有分歧"不等于"高度一致"）。
    """
    if not a or len(a) != len(b):
        return None
    labels = sorted(set(a) | set(b))
    k = len(labels)
    if k < 2:
        return None
    index = {label: i for i, label in enumerate(labels)}
    n = len(a)

    def weight(i: int, j: int) -> float:
        if not weighted:
            return 0.0 if i == j else 1.0
        return ((i - j) / (k - 1)) ** 2

    matrix = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        matrix[index[x]][index[y]] += 1
    row = [sum(matrix[i]) for i in range(k)]
    col = [sum(matrix[i][j] for i in range(k)) for j in range(k)]

    observed = expected = 0.0
    for i in range(k):
        for j in range(k):
            w = weight(i, j)
            observed += w * matrix[i][j] / n
            expected += w * (row[i] / n) * (col[j] / n)
    if expected == 0:
        return None
    return 1.0 - observed / expected

def spearman(a: list[float], b: list[float]) -> float | None:
    """Spearman 秩相关：两组打分的**排序**有多一致（对单调变换不敏感）。

    kappa 看"逐条对不对得上"，spearman 看"排序趋势一致不一致"——裁判整体偏高但
    排序与人工一致时，kappa 会难看而 spearman 仍高。两个都看才不会误判。
    """
    if not a or len(a) != len(b) or len(a) < 2:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1  # 并列取平均秩，否则并列值会人为拉低相关
            for m in range(i, j + 1):
                result[order[m]] = avg
            i = j + 1
        return result

    ra, rb = ranks(a), ranks(b)
    mean_a = sum(ra) / len(ra)
    mean_b = sum(rb) / len(rb)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)
    if var_a == 0 or var_b == 0:
        return None
    return cov / (var_a * var_b) ** 0.5
