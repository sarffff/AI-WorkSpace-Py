"""同一份材料审 N 次，结论不一致就转人工。

## 为什么需要这个

2026-09-12 实测：同一条用例跑三轮，**工具调用序列逐次相同**，裁判分 5/5/3
（``absent-byod``）与 5/3/5（``multi-key-leak-response``）；对照组
``fs-read-single`` 三轮全 5.0。温度是 0.0（``eval/agent_runner.py``）。

两个结论：

1. **摆动在结论层，不在工具层。** 同样的动作、同样的材料、不同的结论。
   所以这里只重跑**结论生成**，不重跑整条工具链——后者每份材料贵 N 倍，
   而实测说明那 N 倍买不到任何东西。
2. **改配置修不了。** 贪心解码本该可复现，而它没有（MoE / 批处理推理常见）。
   所以这不是配置问题，只能在产品层兜住。

复审是审核的基本要求。"同一张单子跑两遍可能给两个结论"在这个场景里是硬伤。

## 判据：比字段，不比文本

比文本会在两头都出错：措辞变了但结论相同（假阳），措辞相同但金额差一位
（假阴）。所以比 ``verdict``，再比每一项 ``found``。

``found`` 的分歧比 ``verdict`` 的分歧更严重，因为它说明两次**看到的材料不一样**
——那时哪个 verdict 对都不重要了。但两者的处置相同（转人工），所以不分级，
只在 ``reasons`` 里说清是哪一种，让人知道该看什么。

``value`` 不参与判据：同一张发票的金额可能被抄成 "1200" 和 "1200.00"，
那是格式差异不是分歧。真正的金额错误会体现在 ``verdict`` 上。

## 不做多数表决

三次里两次 pass 就通过？没有。理由是**这里没有"多数正确"的先验**：实测
5/5/3 与 5/3/5 两种形状都出现过，同一条用例在不同批次里多数票会翻面。
多数表决会把"这题它拿不准"变成"这题它答对了 2/3"，而那正是要避免的结论。

不一致就是拿不准，拿不准就交给人。这也让成本可预测：N 次生成，一次判定。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config import settings
from services.structured import (
    InputCheck,
    ReviewVerdict,
    StructuredReport,
    request_structured,
)

logger = logging.getLogger("review_consensus")


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """N 次结论的合并结果。"""

    # 最终交出去的那一份。一致时是第一次的原样，不一致时是合成的 needs_human。
    verdict: ReviewVerdict
    # 跑了几次。1 表示没做一致性检查（开关关着或只跑了一次）
    runs: int
    agreed: bool
    # 不一致时说清是哪一种分歧。空 = 一致。
    # 这几句会进 basis，所以是面向人的措辞，不是调试信息
    reasons: tuple[str, ...] = ()


def _found_map(verdict: ReviewVerdict) -> dict[str, bool]:
    return {item.name: item.found for item in verdict.inputs}


def combine(verdicts: list[ReviewVerdict]) -> ConsensusResult:
    """把 N 份结论合并成一份。

    空列表是调用方的错（一次都没生成却要合并），抛而不是返回一个假结论——
    这里没有任何"安全的默认值"：给 needs_human 会掩盖调用方的 bug，
    给 pass 更糟。
    """
    if not verdicts:
        raise ValueError("没有可合并的结论")

    first = verdicts[0]
    if len(verdicts) == 1:
        return ConsensusResult(verdict=first, runs=1, agreed=True)

    reasons: list[str] = []

    # 1. verdict 本身
    seen = {v.verdict for v in verdicts}
    if len(seen) > 1:
        counts = ", ".join(
            f"{value}×{sum(1 for v in verdicts if v.verdict == value)}"
            for value in sorted(seen)
        )
        reasons.append(f"{len(verdicts)} 次审核给出了不同结论（{counts}）")

    # 2. 每一项材料"找到了没有"。逐项比而不是比整个 dict：
    #    要在 reasons 里点名是哪一项，人才知道该去核对什么。
    base_found = _found_map(first)
    for name in base_found:
        values = {_found_map(v).get(name) for v in verdicts}
        if len(values) > 1:
            reasons.append(
                f"「{name}」这一项，有的轮次找到了、有的没找到——"
                "两次看到的材料不一样，需要人确认"
            )
    # 项数不同：SOP 声明了 N 项，而某一轮只核对了 M 项。这比某一项分歧更严重
    # （说明结论的形状本身就不对），但处置相同，所以只多记一条
    shapes = {tuple(sorted(_found_map(v))) for v in verdicts}
    if len(shapes) > 1:
        reasons.append("各轮核对的材料项不一致，结论的形状本身对不上")

    if not reasons:
        return ConsensusResult(
            verdict=first, runs=len(verdicts), agreed=True
        )

    logger.info(
        "review consensus disagreed over %s runs: %s",
        len(verdicts),
        "; ".join(reasons),
    )
    return ConsensusResult(
        verdict=_to_human(first, verdicts, reasons),
        runs=len(verdicts),
        agreed=False,
        reasons=tuple(reasons),
    )


def _to_human(
    first: ReviewVerdict, verdicts: list[ReviewVerdict], reasons: list[str]
) -> ReviewVerdict:
    """不一致时合成一份 needs_human。

    ``inputs`` 取**最保守**的那一份：任一轮说没找到，就算没找到。理由是这一列
    是给人复核用的清单，而"有一轮没找到"正是需要人去看的地方；取乐观值会让
    那一项从清单上消失。

    这也顺带满足了 ``ReviewVerdict`` 的 validator：有 found=false 时 verdict
    必须是 needs_human，而这里正是 needs_human。
    """
    names: list[str] = [item.name for item in first.inputs]
    conservative: list[InputCheck] = []
    for name in names:
        found_all = all(_found_map(v).get(name, False) for v in verdicts)
        # value 取第一份里非空的那个，只作展示
        value = next(
            (
                item.value
                for v in verdicts
                for item in v.inputs
                if item.name == name and item.value
            ),
            None,
        )
        conservative.append(
            InputCheck(name=name, value=value, found=found_all)
        )

    # 原来那几份的依据保留下来：人要判断的时候，"它当时是怎么想的"比
    # "它最后说了什么"有用得多。前面加上分歧说明，因为那是人要先看的东西。
    basis = [f"⚠ {reason}" for reason in reasons]
    for index, one in enumerate(verdicts, start=1):
        for line in one.basis:
            basis.append(f"[第 {index} 次] {line}")

    return ReviewVerdict(
        inputs=conservative,
        basis=basis,
        verdict="needs_human",
        sop_name=first.sop_name,
        sop_version=first.sop_version,
    )


async def generate(
    adapter: Any,
    *,
    prompt: str,
    model: str,
    runs: int | None = None,
    max_tokens: int = 2048,
) -> tuple[ConsensusResult | None, list[StructuredReport]]:
    """生成 N 份结论并合并。返回 ``(结果, 每次的报告)``。

    ``None`` 表示一次都没拿到合法结论——调用方据此告诉用户"这次审不了"，
    **不能**退回一个 pass。这是这个模块里最重要的一条：审核失败的正确处置是
    承认失败，而不是给一个听起来合理的结论。

    ## 温度不是 0.0

    这里刻意用 ``REVIEW_CONSENSUS_TEMPERATURE``（默认 0.3）而不是 0.0。听起来
    反直觉，但 0.0 在这里是**有害**的：自洽性检查要的是"独立采样两次看看会不会
    分歧"，而 0.0 是在请求同一条贪心路径。实测说明 0.0 也会摆（5/5/3），所以
    它既拿不到复现性、又拿不到独立性——两头都不占。

    调高之后分歧会**变多**，那正是这个机制该做的事：把本来就不稳的判断暴露出来
    转人工，而不是靠采样一次碰运气。

    ## 部分失败照常合并

    N 次里有几次没跑通（截断、JSON 坏了）时，用拿到的那几份继续。理由是
    "少一个样本"和"结论不一致"是两件事，而前者不该让整次审核失败——但只剩一份时
    ``combine`` 会把 ``runs`` 记成 1，上层于是知道这次实际没检查过。
    """
    total = max(1, runs if runs is not None else settings.REVIEW_CONSENSUS_RUNS)
    verdicts: list[ReviewVerdict] = []
    reports: list[StructuredReport] = []
    for index in range(total):
        value, report = await request_structured(
            adapter,
            schema=ReviewVerdict,
            prompt=prompt,
            model=model,
            purpose="review_verdict",
            array=False,
            temperature=settings.REVIEW_CONSENSUS_TEMPERATURE,
            max_tokens=max_tokens,
        )
        reports.append(report)
        if value is not None:
            verdicts.append(value)
        else:
            # 必须留日志：一次都没成功和"结论一致"在返回值上不同形，但
            # "3 次里 2 次没跑通"会安静地退化成"没做一致性检查"
            logger.warning(
                "review verdict %s/%s produced nothing: attempts=%s finish_reason=%s",
                index + 1,
                total,
                report.attempts,
                report.finish_reason,
            )
    if not verdicts:
        return None, reports
    return combine(verdicts), reports


__all__ = ["ConsensusResult", "combine", "generate"]
