"""显式规划：进执行循环之前先把问题拆成有序步骤。

## 和现在这个循环的关系

现在的循环是 ReAct：调一次工具、看结果、再决定下一步。它的优势恰恰在于**结果
不确定的时候**——检索空了就换个查询，这件事没法预先写进计划。

所以这里不替换那个循环，只在它前面加一段。plan_execute 想补的是另一件事：
**事前的全局视野**。ReAct 一次只想一步，于是在需要"先查 A、再查 B、然后对比"
的问题上容易走成"查 A、答一半、发现还缺 B、再查"，多花轮次而且答案更容易漏一半。
2026-08-22 那轮 multi_domain 实测就是这个形状：模型拿预检索给到的半边作答，
缺的那半不会自己回头去找（keyword_coverage 0.5）。

## 已知的失效模式，以及这里怎么防

plan-and-execute 出名的问题是**给简单问题硬凑步骤**：多一次调用、多几轮，答案
一模一样。两道防线：

1. 提示词明确说"能一步答完就写一步"、"不需要工具就输出 []"，空计划是合法输出。
2. 规划失败**不影响回答**——和检索改写、记忆抽取一样是增强不是依赖。

第 2 条带着这个仓库刚学到的教训：增强的静默降级是最难查的一类故障。本轮实测
七个辅助调用点有五个因为 max_tokens 给小了 100% 返回空串，而每个调用方都
"合理地"降级、没有日志，于是四个检索增强从来没执行过、对应的 eval 变体与
baseline 逐位相同。所以这里从一开始就把话筒装上：失败必打 warning，
预算按思考开销给（``AGENT_PLAN_MAX_TOKENS``），并且 eval 里有一个专门的指标
盯"计划了却没照做"。

## 为什么不自动勾掉步骤

想过在 ``TurnState`` 里放一个 step 游标、每轮往前推一格。没做，因为**没有任何
可靠信号说明"这一步完成了"**：一轮里可能并行调三个工具，也可能一轮什么都没做完。
按轮次推游标是个看起来精确的假数字——和 ``round_efficiency`` 把 min_rounds 标大
之后一样，错了也看不出来。

取而代之的是可验证的东西：计划里点名的工具，实际有没有被调用
（``eval/agent_metrics.plan_adherence``）。这个数不需要模型自报进度，
从已经在收集的事件流里就能算出来。
"""
from __future__ import annotations

import logging
from typing import Any

from config import settings
from services import prompt_library, structured

logger = logging.getLogger("planner")

# 计划注入用的措辞。放在 user 消息而不是系统提示词里,和"已预检索过"那句同一个
# 理由(见 chat_service 里 PROMPT_CACHE_STABLE_PREFIX 那段):系统提示词是整个
# 前缀的第一条消息,让它随本轮计划变化就是把提示词缓存整段作废。
_PLAN_NOTICE = (
    "[开始之前你先做了一份计划，按顺序列在下面。它是给你自己的路线图，"
    "不是必须逐条执行的命令：某一步发现不需要就跳过，发现计划错了就改，"
    "但不要因为计划里没写而漏掉用户真正问的东西。]"
)


def enabled() -> bool:
    return settings.AGENT_PLAN_MODE == "plan_execute"


def format_steps(steps: list[dict[str, Any]]) -> str:
    """把计划渲染成注入用的文本。"""
    lines = []
    for index, step in enumerate(steps, start=1):
        tool = str(step.get("tool") or "").strip()
        suffix = f"（用 {tool}）" if tool else "（不需要工具）"
        lines.append(f"{index}. {step.get('goal', '')}{suffix}")
    return f"{_PLAN_NOTICE}\n" + "\n".join(lines)


async def build_plan(
    model_adapter: Any,
    *,
    question: str,
    context: str,
    tool_names: list[str],
) -> list[dict[str, Any]]:
    """产出一份计划，返回 ``[{goal, tool}, ...]``。

    返回空列表有两种含义，而它们**都不该让回答失败**：模型判断不需要分步
    （正确行为），或者规划这次调用没跑通（故障）。两者在返回值上同形，所以后者
    必须留日志——这正是本仓库里同一类故障能藏很久的原因。

    工具名的合法性在这里查而不是在契约里：可用工具随开关和委派模式变，
    那是调用方才有的信息。编出来的工具名不丢掉整份计划，只把那一步的 tool
    清空——计划的价值在 goal 上，为一个错的工具名废掉整份计划不值得。
    """
    if not enabled():
        return []

    prompt = prompt_library.render(
        "agent_plan",
        question=question[:2000],
        # 已有材料给个摘要就够:规划要判断的是"这部分事实是不是已经拿到了",
        # 不需要全文。而全文塞进去会让规划这次调用的输入和主回答一样贵。
        context=(context[:1500] + "…") if len(context) > 1500 else (context or "（无）"),
        tools=", ".join(tool_names) or "（无可用工具）",
        max_steps=max(1, settings.AGENT_PLAN_MAX_STEPS),
    )
    result, report = await structured.request_structured(
        model_adapter,
        schema=structured.Plan,
        prompt=prompt,
        model=settings.utility_model,
        purpose="agent_plan",
        array=True,
        temperature=0.0,
        max_tokens=settings.AGENT_PLAN_MAX_TOKENS,
    )
    if result is None:
        # 规划是增强不是依赖:失败就退回纯 ReAct,这一轮照常回答。
        # 但必须留日志——"模型判断不需要分步"和"规划根本没跑通"在返回值上
        # 完全同形(都是空计划),而后者是个需要立刻修的故障。
        logger.warning(
            "planning produced no usable plan: attempts=%s failures=%s "
            "finish_reason=%s → falling back to plain ReAct",
            report.attempts,
            report.failures,
            report.finish_reason,
        )
        return []

    allowed = set(tool_names)
    steps: list[dict[str, Any]] = []
    for item in result.items:
        tool = item.tool.strip()
        if tool and tool not in allowed:
            logger.warning("plan referenced unknown tool %r; dropping the tool hint", tool)
            tool = ""
        steps.append({"goal": item.goal, "tool": tool})
    return steps
