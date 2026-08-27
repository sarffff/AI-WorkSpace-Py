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
  它同时也是记忆注入防线（fence + 无指令权限声明）的对照组。
- ``delegation-*``     委派值不值。这是系统里最贵的功能（每次委派多一个完整的
  嵌套子代理循环），而它和 tool-history 一样，上线以来没有数字支持过。
  两个变体分开是因为 augment 与 supervisor 问的不是同一个问题：前者问
  「模型自己会不会在该委派的时候委派」，后者问「强制分工是否比单代理更好」。
- ``no-repeat-guard``  重复调用检测的对照组。它是三个新开关里唯一会改变工具
  调用序列的，所以必须能关掉才量得出它省了多少、有没有误伤。
- ``no-stable-prefix`` 提示词缓存的对照组。它不改变模型收到的约束内容，只改变
  那句约束放在系统提示词还是用户消息里，所以要看的是 ``cache_hit_ratio``
  和成本，不是成功率。
- ``no-structured-retry`` 结构化输出重试的对照组。

注意子代理的工具调用也会计入 ``expect_tools`` / ``forbid_tools``（见
``agent_runner`` 里对 ``agent_step`` 的处理）：任务集问的是「这一轮该不该查
知识库」，而不是「该由谁去查」。谁去查属于委派策略，由 delegate 的出现次数
与轮次成本体现。
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
    # 重复调用检测。必须显式写死:它直接改变工具调用序列(被拦下的那次不执行),
    # 也就直接改变工具精度、轮次效率和成本。本地 .env 把它关掉的话,
    # 每个变体都会静默带上另一套循环行为跑,而报告上看不出任何异常。
    "AGENT_REPEAT_LIMIT": 3,
    # ---- 提示词缓存 ----
    # 它改变系统提示词的正文(prefetched 段进不进),因此改变输入 token 与
    # 模型看到的约束。写死才能保证跨变体可比。
    "PROMPT_CACHE_STABLE_PREFIX": True,
    # ---- 结构化输出 ----
    # 重试会多花一次辅助模型调用,进成本列。写死避免本地配置影响成本对比。
    "STRUCTURED_OUTPUT_RETRIES": 1,
    # ---- 人工审批 ----
    # 审批会在 tool_start 之前拦下 gated 工具那次调用，也就直接改变工具调用
    # 序列——按本节开头的规矩必须写死。
    #
    # 必须是 off:这套评估直接驱动 chat_service,没有任何人会在**另一个请求**里
    # 点同意,所以开着它 write 类任务永远跑不完。而失败方式是静默的——审批门在
    # tool_start 之前,于是 calls 里没有 save_to_knowledge_base（召回 0）、
    # answer 是空的、errors 也是空的,报告上看起来像"模型被明确要求保存却没保存"。
    # 真实原因是评估框架自己把它拦了。
    #
    # 想评审批本身要的是另一种东西:一次被拒绝之后模型会不会改方案
    # （见 approval.rejection_message）。那已经做了,但**不是**把这里打开——
    # ``agent_runner._approval_gate`` 按任务临时开闸,只对声明了 ``approval``
    # 的那几条用例生效,退出即还原。按变体开会把其余 20 多条用例一起废掉。
    "AGENT_APPROVAL_MODE": "off",
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
    # ---- 跨会话长期记忆 ----
    # 必须显式写死。injection-memory 那个任务靠预置记忆行来测注入防线,而本地
    # .env 把 MEMORY_ENABLED 关掉的话:记忆压根不注入 → canary 不可能出现 →
    # 抗注入率满分,同时第二轮的 must_include 会掉。也就是说漏掉这一条时,
    # 报告会同时给出一个假的满分和一个原因不明的失败。
    "MEMORY_ENABLED": True,
    "MEMORY_INJECT_LIMIT": 20,
    # ---- 多代理 ----
    # 必须显式写 off。漏掉这一条时,本地 .env 开了委派的话每个变体都会静默带上它
    # 跑——每次委派多一个完整的嵌套子代理循环,轮次、成本、工具调用全都变了,
    # 而报告上看不出任何异常。这正是本文件开头那条"关键开关全部写全"要防的事。
    "AGENT_DELEGATION_MODE": "off",
    "AGENT_MAX_DELEGATIONS": 3,
    # ---- 显式规划 ----
    # 必须显式写 off,理由和上面委派那条完全一样:规划是进循环之前多一次辅助模型
    # 调用,而且它会往 messages 里多塞一条消息——轮次、成本、工具选择全都会变。
    # 本地 .env 打开它的话每个变体都会静默带上规划跑,报告上看不出任何异常。
    "AGENT_PLAN_MODE": "off",
    "AGENT_PLAN_MAX_STEPS": 5,
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
            "description 撑着。工具选择正确率与输入 token 一起看才有意义。"
            "注意它回答的是「四个工具都开着时 v4 值不值这笔固定 token」——"
            "_BASE 把工具全打开了。产品默认是工具全关，那种配置下 v4 多讲的"
            "三段策略指向的工具根本不存在，所以这组数字不能拿来定产品默认版本"
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
            "有多少是护栏的功劳、多少只是提示词在起作用。"
            "注意这一版同时也关掉了记忆块的定界与声明（build_system_block 会退回"
            "一个静态表头），所以它顺带就是记忆注入防线的对照组"
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
    # 委派是整个系统里最贵的功能——每次多一个完整的嵌套子代理循环，最坏情况
    # AGENT_MAX_DELEGATIONS=3 就是三次。它上线以来没有任何数字支持过它，
    # 和 no-tool-history 当初的处境一样。
    #
    # 提示词必须跟着模式换（v5-augment / v6-supervisor），否则 main.py 的启动
    # 校验会直接拒绝——这里两个变体正好也验证了那套校验和 eval 配置是一致的。
    "delegation-augment": AgentVariant(
        name="delegation-augment",
        description=(
            "主代理保留全部工具 + delegate，自己判断什么时候派人。"
            "要看的是：任务成功率有没有提升，以及为此多付了多少轮和多少 token。"
            "成功率不动而成本上去，就说明这些任务本来不需要委派"
        ),
        overrides={
            **_BASE,
            "AGENT_DELEGATION_MODE": "augment",
            "PROMPT_CHAT_SYSTEM_VERSION": "v5-augment",
        },
    ),
    "delegation-supervisor": AgentVariant(
        name="delegation-supervisor",
        description=(
            "专用工具从主代理手里收走，只能委派。分工最干净，代价是简单问题也要"
            "多付一次子代理循环。和 delegation-augment 对照能看出"
            "「强制委派」和「自主选择委派」差在哪"
        ),
        overrides={
            **_BASE,
            "AGENT_DELEGATION_MODE": "supervisor",
            "PROMPT_CHAT_SYSTEM_VERSION": "v6-supervisor",
        },
    ),
    "no-repeat-guard": AgentVariant(
        name="no-repeat-guard",
        description=(
            "关掉重复调用检测,退回改动前的循环。这是它的对照组:"
            "``repeatedCalls`` 应当上升(那笔浪费重新出现),``repeatedBlocked`` 归零。"
            "要看的是任务成功率和轮次效率——如果成功率不动而轮次效率下降,"
            "检测省下的就是纯浪费;如果成功率跟着掉,说明有任务真的需要重查,"
            "那时该调高 AGENT_REPEAT_LIMIT 而不是关掉它"
        ),
        overrides={**_BASE, "AGENT_REPEAT_LIMIT": 0},
    ),
    "no-stable-prefix": AgentVariant(
        name="no-stable-prefix",
        description=(
            "把「已预检索过」那句话放回系统提示词(改动前的行为)。"
            "系统提示词于是随预检索命中与否在两种正文之间切,前缀缓存跟着作废。"
            "对照的是 trace 里的 cache_hit_ratio 与成本列,不是任务成功率——"
            "两版给模型的约束内容一样,只是位置不同"
        ),
        overrides={**_BASE, "PROMPT_CACHE_STABLE_PREFIX": False},
    ),
    "no-structured-retry": AgentVariant(
        name="no-structured-retry",
        description=(
            "结构化输出校验失败不重试。只在开了多查询改写/重排,或者记忆抽取"
            "真的返回过不合法 JSON 时才看得出差别——baseline 下大概率完全一致,"
            "那本身就是有用的信息:说明这几处的输出一直是干净的,重试是白配的保险"
        ),
        overrides={**_BASE, "STRUCTURED_OUTPUT_RETRIES": 0},
    ),
    # 显式规划。和 delegation-* 同一处境:上线即无数字支持,所以先量再说。
    #
    # 该盯的三个数,顺序不能反:
    #   1. planSteps      规划到底有没有产出。0 步意味着要么模型判断不用分步
    #                     (合法),要么规划调用静默失效了(故障)——两者在指标上
    #                     同形,靠 planner 那条 warning 区分。**这一列是 0 的话
    #                     下面两个数都不用看**。
    #   2. planAdherence  计划点名的工具实际调了几成。低说明计划是装饰。
    #   3. keywordCoverage / avgRounds / 成本
    #      规划想换来的是"少绕路、少漏半边"。multi_domain 那几条第二轮现在有
    #      0.5 的关键词命中,那就是它该补上的余量;而它一定会多花一次调用,
    #      所以成本必然上升。成功率不动而成本上去,就说明这些任务不需要规划。
    "plan-execute": AgentVariant(
        name="plan-execute",
        description=(
            "进执行循环之前先让辅助模型把问题拆成有序步骤，计划作为一条指引注入。"
            "要看的是关键词命中（少漏半边）与轮次成本这笔交易，以及 planSteps "
            "非零——为零就说明规划压根没产出，那时后面的数字都不用读"
        ),
        overrides={**_BASE, "AGENT_PLAN_MODE": "plan_execute"},
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
