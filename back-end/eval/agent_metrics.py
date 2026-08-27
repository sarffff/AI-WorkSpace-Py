"""Agent 循环的评估指标。

和 ``metrics.py`` 一样全是纯函数、零成本、完全确定——先把便宜的信号用尽，
再花钱请 LLM 裁判。检索指标衡量"找到了没有"，这里衡量的是**决策质量**：
该用的工具用了没有、不该用的有没有乱用、绕了几圈才收敛。

关键设计：召回按集合算，精度按调用次数算。

- 召回问的是"必需的工具每个都至少用了一次吗"，同一个工具调两次并不更正确，
  所以按集合去重。
- 精度问的是"你做的这些动作里有多少是必要的"，而每一次多余调用都实打实地
  吃掉一轮预算和一份上下文，所以按次数算、不去重。

只报一个"综合分"是没用的：召回高精度低是"什么都试一遍"，精度高召回低是
"能不动就不动"，两种失败模式的修法完全相反，混成一个数就分不出来了。
"""
from __future__ import annotations

import json
from typing import Any


def canonical_call(name: str, arguments: Any) -> tuple[str, str]:
    """把一次调用压成可比较的键。

    参数按 key 排序后序列化，因为 ``{"a":1,"b":2}`` 和 ``{"b":2,"a":1}`` 是同一次
    调用；不排序会让重复调用统计漏掉一半。
    """
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return name, encoded


def tool_recall(actual: list[str], expected: list[str]) -> float:
    """必需工具的覆盖率。没有必需工具时返回 1.0（无从漏用）。"""
    if not expected:
        return 1.0
    used = set(actual)
    return sum(1 for name in set(expected) if name in used) / len(set(expected))


def tool_precision(actual: list[str], expected: list[str]) -> float | None:
    """实际调用里有多少比例落在必需集合内。

    完全没调工具时返回 ``None`` 而不是 0：那种情况下这个指标是空洞的
    （"你做的事里多少是必要的"——你什么都没做），惩罚由召回承担。
    但如果本来就不该调工具，什么都没调是满分。
    """
    if not actual:
        return 1.0 if not expected else None
    allowed = set(expected)
    return sum(1 for name in actual if name in allowed) / len(actual)


def forbidden_hits(actual: list[str], forbidden: list[str]) -> int:
    """调了几次明确不该调的工具。

    这是唯一一个"越低越好且 0 是硬要求"的指标：``save_to_knowledge_base``
    在用户没要求时被调用一次，就是一次未经许可的状态变更。
    """
    if not forbidden:
        return 0
    banned = set(forbidden)
    return sum(1 for name in actual if name in banned)


def order_respected(actual: list[str], expected: list[str]) -> bool | None:
    """必需工具是否按给定顺序出现（允许中间夹别的调用）。

    子序列匹配而不是全等：中间多调一次检索不影响"先查资料再算数"这个次序是否成立。
    只有数据集显式给了顺序要求时才有意义，否则返回 None。
    """
    if len(expected) < 2:
        return None
    remaining = list(expected)
    for name in actual:
        if remaining and name == remaining[0]:
            remaining.pop(0)
    return not remaining


def round_efficiency(used: int, minimum: int) -> float | None:
    """最少必要轮次 / 实际轮次，上限 1.0。

    ``minimum`` 是"一个正确回答至少需要几轮"，人工标在数据集里。所以 1.0 表示
    没有浪费，0.5 表示绕了一倍的路。它不能超过 1.0——比下限还少的轮次意味着
    标注错了或者答案根本没做该做的事，那属于正确性问题，由召回和裁判去抓。
    """
    if minimum <= 0 or used <= 0:
        return None
    return min(1.0, minimum / used)


def repeated_calls(calls: list[tuple[str, str]]) -> int:
    """同一个 (工具, 参数) 被重复执行了几次（首次不算）。

    循环目前没有重复调用检测：``_ToolResultBudget`` 管的是总字符量，管不了
    "同一个查询连发三轮"。这个数字就是那笔浪费的大小，给后续做去重时留一个
    改动前的基线。

    注意它是诊断值不是错误数：只读工具重复调用纯属浪费，但
    ``save_to_knowledge_base`` 同参数调两次会真的产生两份文档——
    那种情况该由 ``forbidden_hits`` 或裁判抓，不该混进这里当成同一件事。
    """
    return len(calls) - len(set(calls))


def plan_adherence(planned_tools: list[str], actual: list[str]) -> float | None:
    """计划里点名的工具，实际调了几成。

    没开规划、或者计划里一个工具都没点名（纯推理步骤）时返回 ``None`` 而不是
    0 或 1：那种情况下这个指标没有标的，填任何数字都会污染均值。这和
    ``tool_precision`` 对"什么都没调"返回 None 是同一条规矩。

    为什么用"点名的工具调没调"而不是"步骤完成没完成"：**没有可靠信号说明一个
    步骤完成了。** 一轮里可能并行调三个工具，也可能一轮什么都没做完，所以按轮次
    推一个 step 游标只会得到一个看起来精确的假数字（同 ``round_efficiency`` 把
    ``min_rounds`` 标大之后的情形：错了也看不出来）。而"计划说要用 X，事件流里
    有没有 X"是从已经在收集的数据里就能算出来的事实。

    按集合算而不是按次数：计划说"用 search_knowledge_base 查报销时限"，模型查了
    三次不同的查询词，那是执行细节，不是更遵守计划。重复的浪费由
    ``repeated_calls`` 负责。

    读法上要和 ``planSteps`` 一起看：0 步的时候这里是 None，而 0 步既可能是
    "模型判断不用分步"（正确），也可能是"规划调用静默失效了"（故障）。
    """
    wanted = {name for name in planned_tools if name}
    if not wanted:
        return None
    return len(wanted & set(actual)) / len(wanted)
