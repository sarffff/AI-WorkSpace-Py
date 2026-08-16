"""子代理角色注册表。

多代理协作在这里的形态是**委派**，不是预先画好的工作流图：主代理在运行时决定
要不要把子任务交出去、交给谁。之所以不做成图，是因为图把"该谁上"这个决定从
运行时挪到了编码时——那样它就不再是 agent，而是一条带分支的流水线。

一个角色 = 一段系统提示词 + 一个工具子集 + 一个轮次上限。三样都必须有：

- **只给提示词不限工具**，角色就是装饰。让 researcher 拿到 ``calculate``，它
  照样会自己算，于是"分工"只存在于提示词的措辞里。
- **只限工具不给提示词**，模型不知道自己现在的产出要交给谁、该给成什么样。
  子代理的输出是**给主代理读的报告**，不是给用户看的回答，这件事必须说明。
- **不限轮次**，一次委派就能把整个回合的预算烧完。子代理拿的是共享预算
  （见 chat_service 里 ``_ToolResultBudget`` 的传递），所以它必须自己有上限。

写操作（``save_to_knowledge_base``）不给任何角色。它需要用户明确要求才能执行，
而委派会把"谁要求的"这件事隔一层：子代理只看到一句任务描述，看不到用户原话，
判断不了"用户是否真的要求保存"。这类工具留在主代理手里。
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
    # 允许使用的工具名。空元组表示纯推理角色（critic 就是）。
    # 实际下发的是它与"本轮真正注册了的工具"的交集：知识库关着的时候
    # researcher 不该收到 search_knowledge_base 的 schema。
    tools: tuple[str, ...]
    # 该角色自己的最大模型轮次。最后一轮不下发工具，强制它给出报告。
    max_rounds: int


ROLES: dict[str, AgentRole] = {
    "researcher": AgentRole(
        name="researcher",
        summary=(
            "查资料。可检索本地知识库与互联网、按 document_id 定向读取分块。"
            "只负责把事实与出处找齐并如实汇报，不做算术、不写文件、不下结论。"
        ),
        prompt_key="agent_researcher",
        tools=(
            "search_knowledge_base",
            "list_knowledge_documents",
            "read_document_chunk",
            "web_search",
        ),
        max_rounds=4,
    ),
    "analyst": AgentRole(
        name="analyst",
        summary=(
            "算数与读附件。可对给定材料做精确计算、读取 /uploads/... 附件全文。"
            "不检索知识库也不上网——需要的材料要在任务描述里给它。"
        ),
        prompt_key="agent_analyst",
        tools=("calculate", "read_attachment"),
        max_rounds=3,
    ),
    "critic": AgentRole(
        name="critic",
        summary=(
            "审查草稿。没有任何工具，只依据任务描述里给出的材料指出事实错误、"
            "缺失的出处、以及被材料证伪的说法。适合在给出最终回答前过一遍。"
        ),
        prompt_key="agent_critic",
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

    有工具需求但一个都没注册的角色直接排除——``web_search`` 和知识库全关时，
    researcher 只能空手回来，而主代理会先花一轮把任务派给它才发现这件事。
    ``critic`` 不需要工具，所以永远可用。
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
