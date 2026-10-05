"""子代理角色注册表。

多代理协作在这里的形态是**委派**，不是预先画好的工作流图：主代理在运行时决定
要不要把子任务交出去、交给谁。之所以不做成图，是因为图把"该谁上"这个决定从
运行时挪到了编码时——那样它就不再是 agent，而是一条带分支的流水线。

文档第三节把编排层写成「主控 Agent（Router + Planner）+ 子 Agent（查询、操作、
政策、情绪安抚）」。这里落地查询、政策、情绪安抚三个，第四个刻意不做成子代理，
理由写在最后一段。

一个角色 = 一段系统提示词 + 一个工具子集 + 一个轮次上限。三样都必须有：

- **只给提示词不限工具**，角色就是装饰。让 inquiry 拿到 ``create_refund``，它
  照样会自己退，于是"分工"只存在于提示词的措辞里。
- **只限工具不给提示词**，模型不知道自己现在的产出要交给谁、该给成什么样。
  子代理的输出是**给主代理读的报告**，不是给客户的答复，这件事必须说明。
- **不限轮次**，一次委派就能把整张工单的预算烧完。子代理拿的是共享预算
  （由调用方把 ``take_budget`` 递进来），所以它必须自己有上限。

**「操作」那一档为什么不做成子代理。** 委派出去的执行**不过人在回路**：审批闸门在
主循环里按 ``reason`` 节点提出的 proposals 判（见 ``ticket.graph.route_after_reason``），
而子代理跑在一个工具处理器的调用栈里——它执行完任何东西都已经执行完了。更糟的是
``create_refund`` 走那条路会把 ``approved_by`` 填成当前坐席的 id，账本上于是留下一笔
"看起来有人批过"的退款。文档§6.1 要求资金类"高权限 + 人审 + 每日限额"，委派会同时
废掉前两条（限额那条仍然生效，因为它在账本那侧）。

所以写操作留在主代理手里。子代理能做的是**把写操作需要的事实查齐**——查到的可退
余额、订单状态、政策依据，正是人在工单台上点头之前要看的东西。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentRole:
    """一个可被委派的子代理角色。"""

    name: str
    # 写进 delegate 工具的参数描述里，主代理靠它选人。写清"能干什么"和
    # "不能干什么"——只写前者，模型会把所有活都派给第一个角色。
    summary: str
    # prompts/<prompt_key>/<version>.md。和主提示词同一套版本化机制:
    # 子代理的提示词同样是最该被 A/B 的东西，没道理让它退回成源码里的字符串。
    prompt_key: str
    # 允许使用的工具名。空元组表示纯推理角色（reassurance 就是）。
    # 实际下发的是它与"本轮真正注册了的工具"的交集：没接政策检索的时候
    # policy 角色不该收到 search_policy 的 schema。
    tools: tuple[str, ...]
    # 该角色自己的最大模型轮次。最后一轮不下发工具，强制它给出报告。
    max_rounds: int


ROLES: dict[str, AgentRole] = {
    # 文档§3 的「查询」。三个 lookup 工具都在 READ 档（见 ticket.tools.TIER_BY_TOOL），
    # 所以委派它们不改变任何权限边界——主代理自己能调的，它也能调。
    "inquiry": AgentRole(
        name="inquiry",
        summary=(
            "查事实。按订单号查订单、按订单号或运单号查物流、按邮箱/手机号查客户档案。"
            "只把查到的编号、状态、金额、可退余额如实汇报并注明是哪个工具给的，"
            "不判断该不该退、不起草给客户的答复。"
        ),
        prompt_key="ticket_sub_inquiry",
        tools=("lookup_order", "lookup_logistics", "lookup_customer"),
        # 4 轮 = 3 个工具轮 + 最后一轮写报告（最后一轮不下发工具）。
        # 一个委派通常是"查这张单 + 查它的物流"两次调用，留一次换参数的余地。
        max_rounds=4,
    ),
    # 文档§3 的「政策」。它的工具是知识库检索通道，和 retrieve 节点预检索的那次
    # 是同一个后端——差别在于这次是**看完工单之后**发起的：客户问"退货运费谁出"
    # 时，预先按整段原文检索的那一版多半没命中这一条。
    "policy": AgentRole(
        name="policy",
        summary=(
            "查依据。检索知识库里的退换货政策、服务条款与费用规则，报告条款原文与出处。"
            "不碰订单系统，不替客户做决定，也不要把政策改写成承诺。"
        ),
        prompt_key="ticket_sub_policy",
        tools=("search_policy",),
        max_rounds=4,
    ),
    # 文档§3 的「情绪安抚」。没有工具，因为它的输入全在任务描述里（主代理已经把
    # 查到的事实写进去了），而它值钱的地方是**一段干净的草稿**：主代理此刻手上
    # 挂着订单、政策、历史工单三堆材料，让它再兼顾措辞是强人所难。
    #
    # 1 轮：不下发工具，所以第一次生成就必须是成稿。给它 2 轮只是多付一次生成——
    # 没有新信息的第二轮会重写一遍同样漂亮但同样没依据的话。
    "reassurance": AgentRole(
        name="reassurance",
        summary=(
            "起草安抚。没有任何工具，只依据任务描述里给出的事实，为情绪激动的客户"
            "写一段能直接放进答复的中文草稿，并列出其中哪几句需要主代理核实。"
            "不查证、不承诺退款或时效、不替客户决定要不要退。"
        ),
        prompt_key="ticket_sub_reassurance",
        tools=(),
        max_rounds=1,
    ),
}


def get(name: str) -> AgentRole | None:
    return ROLES.get(name)


def names() -> list[str]:
    return list(ROLES)


def available(registered_tools: set[str]) -> list[AgentRole]:
    """在当前工具面下真正有意义的角色。

    有工具需求但一个都没注册的角色直接排除——没接政策检索的时候 policy 只能空手
    回来，而主代理会先花一轮把任务派给它才发现这件事。
    ``reassurance`` 不需要工具，所以永远可用。
    """
    result: list[AgentRole] = []
    for role in ROLES.values():
        if not role.tools:
            result.append(role)
            continue
        if registered_tools & set(role.tools):
            result.append(role)
    return result


def allowed_tools(role: AgentRole, registered_tools: set[str]) -> list[str]:
    """该角色本轮实际能用的工具名，保持 ``role.tools`` 里声明的顺序。"""
    return [name for name in role.tools if name in registered_tools]
