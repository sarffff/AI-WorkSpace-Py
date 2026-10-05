"""子代理角色与 ``delegate`` 的契约。

这里的断言分两类，都是"改了会静默失效"的那类：

1. **角色的工具名必须是真的。** 拼错一个工具名不会报错——``allowed_tools`` 取交集
   时把它悄悄丢掉，于是这个角色少一个工具，而它的提示词还在讲那个工具。
   表现是"派给 inquiry 的任务回来一句我没查到"，而没人知道自己给的活它根本干不了。
2. **``delegate`` 的 schema 只讲契约、不讲策略**（策略在 ``delegation_notice``，
   理由见那个函数的说明），而且角色清单与 enum 必须由同一个列表生成。

写操作不进任何角色，这是文档§6.1 的权限分级在编排层的落点，所以它有一条独立断言。
"""
from __future__ import annotations

import pytest

from services import agent_roles, prompt_library, subagent
from services.ticket import tools as cs


def _schema_for_all_roles():
    return subagent.build_delegate_schema(list(agent_roles.ROLES.values()))[0]


# ========== 角色本身 ==========


def test_role_tools_are_all_real():
    """角色声明的每个工具名都必须在能力层真的存在。

    拼错的名字不会炸：取交集时它只是消失，于是角色的能力比它的说明少一截。
    """
    real = set(cs.TIER_BY_TOOL) | cs.AUXILIARY_READ_TOOLS
    for role in agent_roles.ROLES.values():
        unknown = sorted(set(role.tools) - real)
        assert not unknown, f"{role.name} 声明了不存在的工具 {unknown}"


def test_write_tier_tools_are_in_no_role():
    """修改类与资金类工具不属于任何角色——文档§6.1 要资金类"高权限 + 人审"，
    而委派出去的执行不过 ``await_approval`` 那道闸门（它在主循环按 proposals 判）。

    这条断言的作用域是"现在"：以后有人给 inquiry 加一个 ``update_order_address``
    就能让子代理改单而没人批，而代码读起来完全正常。
    """
    writable = {
        name
        for name, tier in cs.TIER_BY_TOOL.items()
        if tier != cs.READ
    }
    for role in agent_roles.ROLES.values():
        overlap = sorted(set(role.tools) & writable)
        assert not overlap, f"{role.name} 拿到了需要人审的工具 {overlap}"


def test_roles_are_the_ones_the_doc_names():
    """文档§3 的编排层写的是「子 Agent（查询、操作、政策、情绪安抚）」。

    这里落地三个，第四个（操作）刻意不做成子代理——理由与后果都写在
    ``agent_roles`` 的模块文档里。这条断言的意义是**让那个缺席可见**：
    四个名字摆在这儿，少的那一个必须是有人想过的。
    """
    assert set(agent_roles.names()) == {"inquiry", "policy", "reassurance"}


def test_every_role_has_a_prompt_spec_and_a_budget():
    for role in agent_roles.ROLES.values():
        assert role.prompt_key in prompt_library.SPECS, role.name
        # 最后一轮不下发工具，所以 max_rounds=1 的角色必须是纯推理角色
        if role.tools:
            assert role.max_rounds >= 2, f"{role.name} 连一次工具轮都拿不到"


def test_available_drops_roles_whose_tools_are_all_unregistered():
    """没接政策检索时 policy 只能空手回来，而主代理要先花一轮派它才发现。"""
    names = {role.name for role in agent_roles.available({"lookup_order"})}
    assert names == {"inquiry", "reassurance"}

    names = {
        role.name
        for role in agent_roles.available({"lookup_order", "search_policy"})
    }
    assert names == {"inquiry", "policy", "reassurance"}


def test_reassurance_is_always_available():
    """纯推理角色没有工具前提，任何工具面下都能派——它也是最常用到的那一个
    （文档§6.2 把"客户情绪极度负面"列为人工触发条件，起草安抚正是主代理
    在挂起等人之前最该做的事）。"""
    assert "reassurance" in {role.name for role in agent_roles.available(set())}


def test_allowed_tools_intersects_with_registered():
    role = agent_roles.ROLES["inquiry"]
    allowed = agent_roles.allowed_tools(role, {"lookup_order", "create_refund"})
    assert allowed == ["lookup_order"]


def test_allowed_tools_keeps_declared_order():
    """顺序是 ``role.tools`` 里声明的顺序：模型看到的面应当稳定，
    而注册表遍历顺序跟着字典写法抖的话，每轮的前缀都不一样。"""
    role = agent_roles.ROLES["inquiry"]
    registered = {"lookup_customer", "lookup_order", "lookup_logistics"}
    assert agent_roles.allowed_tools(role, registered) == list(role.tools)


# ========== delegate 的 schema ==========


def test_delegate_role_enum_matches_registry():
    assert set(_schema_for_all_roles()["properties"]["role"]["enum"]) == set(
        agent_roles.names()
    )


def test_delegate_description_lists_every_role():
    _schema, description = subagent.build_delegate_schema(
        list(agent_roles.ROLES.values())
    )
    for role in agent_roles.ROLES.values():
        assert role.name in description
        assert role.summary in description


def test_delegate_description_states_the_isolation_fact():
    """子代理看不到这张工单，所以 task 必须自包含——这句话是 ``delegate``
    最要紧的信息，缺了它主代理会写出「帮我查一下这个」这种任务。"""
    _schema, description = subagent.build_delegate_schema(
        list(agent_roles.ROLES.values())
    )
    assert "看不到这张工单" in description


def test_delegate_description_carries_no_policy():
    """契约归 schema，策略归 ``delegation_notice``。

    反过来说：schema 里出现一句"什么时候该委派"就是错的，因为那段策略取决于
    此刻注册了哪些工具（supervisor 下主代理没有查询工具），而这段 description
    说不清一件随配置变化的事。
    """
    _schema, description = subagent.build_delegate_schema(
        list(agent_roles.ROLES.values())
    )
    for phrase in ("不要委派", "应当委派", "值得委派"):
        assert phrase not in description


def test_task_parameter_shows_a_good_and_a_bad_example():
    """``task`` 是整个工具面里唯一一个"写得含糊就必然查错方向"的参数，
    而模型从报错里看不出自己的描述含糊——报错只会来自一个跑偏的检索。
    一个反例比三句叮嘱更能说明"含糊"长什么样。"""
    description = _schema_for_all_roles()["properties"]["task"]["description"]
    assert "好的例子" in description and "不好的例子" in description


# ========== 策略说明（delegation_notice） ==========


def test_notice_lists_exactly_the_roles_that_are_registered():
    roles = [agent_roles.ROLES["inquiry"], agent_roles.ROLES["policy"]]
    notice = subagent.delegation_notice(roles, "augment")
    assert "inquiry" in notice and "policy" in notice
    assert "reassurance" not in notice, "没注册的角色出现在清单里等于邀请模型去派一个不存在的活"


def test_notice_is_empty_without_roles():
    assert subagent.delegation_notice([], "augment") == ""


def test_notice_states_the_write_boundary():
    """策略那段必须明写"写操作不委派"。

    角色拿不到写工具是结构上的保证（``allowed_tools`` 的交集 + 越权回灌），
    但模型看不见那个交集——它会试着派，拿回一句失败，再试一次。这句话省的是
    那两轮的冤枉钱。
    """
    notice = subagent.delegation_notice(list(agent_roles.ROLES.values()), "augment")
    assert "写操作" in notice and "人" in notice


def test_supervisor_notice_does_not_tell_it_to_do_itself():
    """两种模式的策略是矛盾的：augment 下"能自己查的就自己查"是对的，
    supervisor 下主代理没有查询工具，那句话会把模型推进一次注定失败的尝试。
    """
    roles = list(agent_roles.ROLES.values())
    augment = subagent.delegation_notice(roles, "augment")
    supervisor = subagent.delegation_notice(roles, "supervisor")
    assert "自己做" in augment
    assert "自己做" not in supervisor
    assert "没有查询工具" in supervisor


# ========== 模式开关 ==========


@pytest.mark.parametrize(
    "mode,on", [("off", False), ("augment", True), ("supervisor", True), ("nonsense", False)]
)
def test_enabled_only_for_the_two_real_modes(monkeypatch, mode, on):
    from config import settings

    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", mode)
    assert subagent.enabled() is on


def test_describe_mode_names_the_roles(monkeypatch):
    """启动日志那一行要能回答"这个进程现在能派谁"。"""
    from config import settings

    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    described = subagent.describe_mode()
    assert described.startswith("augment")
    for name in agent_roles.names():
        assert name in described
