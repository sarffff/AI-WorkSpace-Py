"""Agent 评估的配置变体。

和 ``variants.py`` 同一个规矩：一个变体只相对 baseline 动一到两个开关，
关键开关全部写全（包括与 baseline 相同的值），这样一次运行的配置完全由代码决定，
换台机器、换个 .env 也能比。

这里的变体是按「哪些问题此前答不上来」挑的，不是把开关列表遍历一遍：

- ``no-tool-history``  第一步（轨迹持久化 + 回灌）到底值不值。这是本套评估
  存在的首要理由——那个功能上线以来没有任何数字支持过它。
- ``prompt-v2``        ``v4-workspace`` 比 ``v2`` 长出一大截，每轮都要付这笔
  固定成本。它换来的工具决策质量是否抵得上，只能这么量。
- ``no-prefetch``      预检索是"每轮固定一次检索"换"模型不查也能看到资料"。
  纯 agentic RAG 到底差多少。
- ``rounds-3``         轮次上限从 6 砍到 3。如果指标不掉，说明 6 是白给的余量。
- ``no-guardrail``     抗注入率里有多少是护栏的功劳、多少只是提示词在起作用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentVariant:
    name: str
    description: str
    overrides: dict[str, Any] = field(default_factory=dict)


_BASE: dict[str, Any] = {
    # ---- 工具面 ----
    # workspace 四个工具在产品里默认关闭（打开一个工具就是打开它的失败模式和
    # 攻击面）。评估必须显式打开，否则评的是一个没有工具的模型。
    "TOOL_CALCULATE_ENABLED": True,
    "TOOL_WEB_SEARCH_ENABLED": True,
    "TOOL_READ_ATTACHMENT_ENABLED": True,
    "TOOL_WRITE_KNOWLEDGE_ENABLED": True,
    # ---- 跨回合记忆 ----
    "TOOL_HISTORY_ENABLED": True,
    "TOOL_HISTORY_TOKEN_BUDGET": 600,
    # ---- 循环 ----
    "AGENT_MAX_TOOL_ROUNDS": 6,
    "TOOL_RESULT_MAX_CHARS": 4000,
    "TOOL_RESULT_TOTAL_CHARS": 12000,
    # ---- 检索 ----
    "RAG_PREFETCH": True,
    "RAG_HYBRID": True,
    "RAG_TOP_K": 5,
    "RAG_CONTEXT_WINDOW": 1,
    "RAG_MULTI_QUERY": False,
    "RAG_RERANK": False,
    "CHUNK_MAX_TOKENS": 320,
    # ---- 历史 ----
    # 多轮任务的第二轮必须能看到第一轮，预算给足；这条被压缩就等于在测别的东西。
    "HISTORY_TOKEN_BUDGET": 4000,
    "HISTORY_SUMMARY": True,
    # ---- 护栏 ----
    "GUARDRAIL_ENABLED": True,
    "GUARDRAIL_BLOCK_SCORE": 0,
    # ---- 提示词 ----
    "PROMPT_CHAT_SYSTEM_VERSION": "v4-workspace",
    # ---- 语义缓存：必须关 ----
    # 命中一次就直接返回存好的答案：0 轮、0 次工具调用、满分轮次效率。
    # 每一个 Agent 指标都会读成"完美且免费"。这是唯一一个不能拿来做变体维度的
    # 开关——它不改变 Agent 的行为，它是绕过 Agent。
    "SEMANTIC_CACHE_ENABLED": False,
}


AGENT_VARIANTS: dict[str, AgentVariant] = {
    "baseline": AgentVariant(
        name="baseline",
        description="当前默认配置 + 全部工具打开 + v4-workspace 提示词",
        overrides=dict(_BASE),
    ),
    "no-tool-history": AgentVariant(
        name="no-tool-history",
        description=(
            "关掉工具轨迹回灌——退回「每个回合从零开始」。memory 探针应当明显下降，"
            "单轮探针不该有变化；如果单轮也变了，说明轨迹块串进了不该进的回合"
        ),
        overrides={**_BASE, "TOOL_HISTORY_ENABLED": False},
    ),
    "prompt-v2": AgentVariant(
        name="prompt-v2",
        description=(
            "换回 v2 系统提示词：只讲了知识库那三个工具，新工具全靠 schema 的 "
            "description 撑着。工具选择正确率与输入 token 一起看才有意义"
        ),
        overrides={**_BASE, "PROMPT_CHAT_SYSTEM_VERSION": "v2"},
    ),
    "prompt-v3-lean": AgentVariant(
        name="prompt-v3-lean",
        description="最短的那一版提示词，看压缩到极限之后先坏在哪个探针上",
        overrides={**_BASE, "PROMPT_CHAT_SYSTEM_VERSION": "v3-lean"},
    ),
    "no-prefetch": AgentVariant(
        name="no-prefetch",
        description=(
            "关掉预检索，纯 agentic RAG：模型不主动查就什么资料都没有。"
            "轮次会上升（多一轮去检索），要看的是任务成功率有没有跟着掉"
        ),
        overrides={**_BASE, "RAG_PREFETCH": False},
    ),
    "rounds-3": AgentVariant(
        name="rounds-3",
        description=(
            "轮次上限从 6 砍到 3。指标不掉说明 6 是白给的余量；"
            "掉了就能看出哪类任务真的需要更多轮"
        ),
        overrides={**_BASE, "AGENT_MAX_TOOL_ROUNDS": 3},
    ),
    "no-guardrail": AgentVariant(
        name="no-guardrail",
        description=(
            "关掉提示注入护栏。和 baseline 对照才知道抗注入率里"
            "有多少是护栏的功劳、多少只是提示词在起作用"
        ),
        overrides={**_BASE, "GUARDRAIL_ENABLED": False},
    ),
    "guardrail-blocking": AgentVariant(
        name="guardrail-blocking",
        description=(
            "可疑资料整段拒绝注入（阈值 5）。抗注入率应当最高，"
            "同时要盯住其它探针的成功率有没有因为误报而掉下来"
        ),
        overrides={**_BASE, "GUARDRAIL_BLOCK_SCORE": 5},
    ),
}


def resolve(names: list[str] | None) -> list[AgentVariant]:
    if not names:
        return [AGENT_VARIANTS["baseline"]]
    if len(names) == 1 and names[0] == "all":
        return list(AGENT_VARIANTS.values())
    unknown = [name for name in names if name not in AGENT_VARIANTS]
    if unknown:
        raise SystemExit(
            f"未知变体: {', '.join(unknown)}\n可用: {', '.join(AGENT_VARIANTS)} 或 all"
        )
    return [AGENT_VARIANTS[name] for name in names]
