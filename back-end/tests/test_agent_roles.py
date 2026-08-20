"""子代理角色与 delegate 契约。

这里钉的是**两处手工对齐**：角色声明的能力要和它真正拿到的工具一致，
delegate 的 schema 要只讲契约、不讲策略（策略归版本化的系统提示词）。
两者漂了都不会报错，只会让主代理按错误的前提规划——最难查的那类问题。
"""
from __future__ import annotations

import re

from services import agent_roles, prompt_library
from services.subagent import build_delegate_schema

ALL_TOOLS = {
    "search_knowledge_base",
    "list_knowledge_documents",
    "read_document_chunk",
    "web_search",
    "calculate",
    "read_attachment",
    "save_to_knowledge_base",
}

# 角色摘要里出现这些词，就应当真的持有对应的工具。摘要是主代理选人的唯一依据
# （它看不到角色的提示词），说了却没有等于让它派错人然后白烧一轮。
CAPABILITY_WORDS = {
    "知识库": {"search_knowledge_base", "list_knowledge_documents", "read_document_chunk"},
    "互联网": {"web_search"},
    "计算": {"calculate"},
    "附件": {"read_attachment"},
}

_DENIALS = ("不", "无法", "不能", "不会", "别")


def _is_denied(summary: str, word: str) -> bool:
    """能力词以否定形式出现（如 analyst 的"不检索知识库"）时不算能力声明。"""
    match = re.search(word, summary)
    if not match:
        return False
    before = summary[max(0, match.start() - 6) : match.start()]
    return any(token in before for token in _DENIALS)


def test_no_role_can_write():
    """写操作需要用户明确要求，而子代理看不到用户原话。"""
    for role in agent_roles.ROLES.values():
        assert "save_to_knowledge_base" not in role.tools, role.name


def test_role_tools_are_all_real():
    for role in agent_roles.ROLES.values():
        unknown = set(role.tools) - ALL_TOOLS
        assert not unknown, f"{role.name} 声明了不存在的工具 {unknown}"


def test_role_summary_matches_its_tools():
    """摘要说得到的能力，必须在 tools 里真的有。

    critic 的摘要写的是"没有任何工具"，所以它不会命中任何能力词——这条对它
    是空过，正确。
    """
    for role in agent_roles.ROLES.values():
        for word, needed in CAPABILITY_WORDS.items():
            if word not in role.summary:
                continue
            if _is_denied(role.summary, word):
                continue
            assert needed & set(role.tools), (
                f"{role.name} 的摘要提到「{word}」，但它没有任何 {needed} 工具"
            )


def test_denied_capability_is_not_checked():
    """analyst 的摘要写的是"不检索知识库"——声明不做，不是声明能力。

    这条钉住否定判断本身：如果摘要哪天改成肯定句（比如真的给了 KB 工具），
    能力词就必须重新生效。
    """
    analyst = agent_roles.ROLES["analyst"]
    assert _is_denied(analyst.summary, "知识库")
    researcher = agent_roles.ROLES["researcher"]
    assert not _is_denied(researcher.summary, "知识库")


def test_every_role_has_a_prompt():
    for role in agent_roles.ROLES.values():
        template = prompt_library.get(role.prompt_key)
        assert template.body.strip(), role.name


def test_roles_have_a_round_cap():
    """不限轮次的话，一次委派就能把整个回合的共享预算烧完。"""
    for role in agent_roles.ROLES.values():
        assert role.max_rounds >= 1, role.name
        assert role.max_rounds <= 6, f"{role.name} 的轮次上限过高"


def test_available_drops_roles_whose_tools_are_all_off():
    """工具全关时 researcher 只能空手回来，而主代理要先派一轮才发现。"""
    names = {role.name for role in agent_roles.available(set())}
    assert names == {"critic"}, "没有工具时只有纯推理角色可用"

    names = {role.name for role in agent_roles.available({"calculate"})}
    assert "analyst" in names and "researcher" not in names


def test_allowed_tools_intersects_with_registered():
    role = agent_roles.ROLES["researcher"]
    allowed = agent_roles.allowed_tools(role, {"web_search", "calculate"})
    # 只保留该角色声明过、且本轮真的注册了的
    assert allowed == ["web_search"]


# ========== delegate 的 schema ==========


def _schema_and_description():
    roles = list(agent_roles.ROLES.values())
    return build_delegate_schema(roles)


def test_delegate_role_enum_matches_registry():
    schema, _description = _schema_and_description()
    assert set(schema["properties"]["role"]["enum"]) == set(agent_roles.names())
    assert schema["required"] == ["role", "task"]
    assert schema["additionalProperties"] is False


def test_delegate_description_lists_every_role():
    _schema, description = _schema_and_description()
    for role in agent_roles.ROLES.values():
        assert role.name in description
        assert role.summary in description


def test_delegate_description_states_the_isolation_fact():
    """"子代理看不到对话"是随模式不变的事实，必须留在 schema 里。"""
    _schema, description = _schema_and_description()
    assert "看不到本次对话" in description


def test_delegate_description_carries_no_policy():
    """策略归系统提示词——同一件事写两处，改一处就会矛盾。

    尤其是"能直接调工具解决的事自己做"：它在 supervisor 模式下是错的，
    那时主代理根本没有那些工具。
    """
    _schema, description = _schema_and_description()
    for banned in ("适合委派", "不要为了", "自己做", "多付一次"):
        assert banned not in description, f"策略措辞「{banned}」应当留在提示词里"


def test_delegation_policy_lives_in_the_prompt_versions():
    """反过来钉住：策略必须在提示词里存在，不能两边都没有。"""
    for version in ("v5-augment", "v6-supervisor"):
        body = prompt_library.get("chat_system_rag", version).body
        assert "自包含" in body, version
        assert "委派" in body, version


def test_task_param_shows_a_good_and_a_bad_example():
    """task 是唯一"写法决定成败"的参数，一个反例胜过三句叮嘱。"""
    schema, _description = _schema_and_description()
    task_description = schema["properties"]["task"]["description"]
    assert "好的例子" in task_description
    assert "不好的例子" in task_description
    # 反例要真的示范"含糊"，而不是又一句抽象叮嘱
    assert "这个" in task_description
