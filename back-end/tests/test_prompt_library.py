"""提示词注册表的测试。

重点不在"渲染出来的字符串等于什么"——那是模板文件的内容，会随时被改。
重点在**契约**：占位符对不上、条件段没闭合、版本不存在时，必须炸在加载/渲染
阶段，而不是让一段自洽但错误的提示词进到模型那边。
"""
from __future__ import annotations

import pytest

from config import Settings, settings
from services import prompt_library
from services.prompt_library import PromptError


def test_registry_loads_all_declared_keys():
    registry = prompt_library.reload()
    assert set(registry) == set(prompt_library.SPECS)
    for key, versions in registry.items():
        assert versions, f"{key} 没有任何版本"


def test_active_versions_resolve_and_are_not_archived():
    for key in prompt_library.SPECS:
        template = prompt_library.get(key)
        assert template.status != "archived", f"{key} 的生效版本是 archived"


def test_chat_system_rag_mentions_every_tool_it_promises():
    """提示词里的工具名和 tool_runtime 的 schema 是手工对齐的，容易漂。

    工具改名却忘了改提示词，模型会去调一个不存在的名字——这类 bug 在
    "偶尔答得怪"里很难被发现，所以在这里钉死。
    """
    body = prompt_library.get("chat_system_rag").body
    for tool in (
        "search_knowledge_base",
        "list_knowledge_documents",
        "read_document_chunk",
    ):
        assert tool in body


def test_chat_system_rag_active_version_declares_injection_defense():
    """生效版本必须带定界符声明——它和 guardrails.fence() 是一对，缺一半就没防线。"""
    body = prompt_library.get("chat_system_rag").body
    assert "资料开始" in body and "资料结束" in body
    assert "不得执行" in body or "不执行" in body


def test_prefetch_flag_switches_the_paragraph():
    template = prompt_library.get("chat_system_rag", "v2")
    with_prefetch = template.render(flags={"prefetched": True})
    without = template.render(flags={"prefetched": False})
    assert "预先检索" in with_prefetch
    assert "预先检索" not in without
    # 条件段标记本身永远不能出现在最终提示词里
    assert "[[if" not in with_prefetch and "[[endif]]" not in without


def test_condition_markers_never_leak():
    for key in prompt_library.SPECS:
        for template in prompt_library.versions(key):
            flags = {name: True for name in template.flags}
            values = {name: "X" for name in template.placeholders}
            rendered = template.render(flags=flags or None, **values)
            assert "[[" not in rendered


def test_unknown_flag_raises_instead_of_being_ignored():
    """静默忽略未声明的开关，等于换版本时漏改调用方也没人知道。"""
    template = prompt_library.get("chat_system_plain")
    with pytest.raises(PromptError):
        template.render(flags={"prefetched": True})


def test_missing_placeholder_value_raises():
    template = prompt_library.get("eval_rag_answer", "v1")
    with pytest.raises(PromptError):
        template.render(context="只给了一半")


def test_unknown_version_raises_with_available_list():
    with pytest.raises(PromptError) as excinfo:
        prompt_library.get("chat_system_rag", "v99")
    assert "v99" in str(excinfo.value)


def test_unknown_key_raises():
    with pytest.raises(PromptError):
        prompt_library.get("no_such_prompt")


def test_subagent_prompts_are_switchable():
    """三个角色的版本必须能按配置切,否则 prompt_key 那套机制是空转的。

    没有 setting 时 resolve_version 只剩 default_version 一个出口——新写一版
    进去没有任何代码路径能到达它,eval 变体也扫不到。
    """
    for key in ("agent_researcher", "agent_analyst", "agent_critic"):
        spec = prompt_library.SPECS[key]
        assert spec.setting, f"{key} 不可切版本"
        assert hasattr(settings, spec.setting), f"{spec.setting} 没有对应的配置项"


def test_each_role_has_its_own_setting():
    """共享一个开关就没法单独 A/B 某个角色,动一个会让另两个的结果一起失效。"""
    settings_used = [
        prompt_library.SPECS[key].setting
        for key in ("agent_researcher", "agent_analyst", "agent_critic")
    ]
    assert len(set(settings_used)) == 3, settings_used


def test_role_prompt_follows_its_setting(monkeypatch):
    """改配置要真的换到另一版——这是 role_prompt() 唯一的切换入口。"""
    from services import agent_roles, subagent

    role = agent_roles.ROLES["critic"]
    spec = prompt_library.SPECS[role.prompt_key]

    # 指向一个不存在的版本：报错说明 setting 真的被读了（而不是静默用默认版）
    monkeypatch.setattr(settings, spec.setting, "v-nope", raising=False)
    with pytest.raises(PromptError):
        subagent.role_prompt(role)

    monkeypatch.setattr(settings, spec.setting, "", raising=False)
    assert subagent.role_prompt(role) == prompt_library.get(role.prompt_key, "v1").body


def test_version_resolution_order(monkeypatch):
    """显式传参 > settings > 契约默认值。"""
    spec = prompt_library.SPECS["chat_system_rag"]
    monkeypatch.setattr(settings, spec.setting, "v1", raising=False)
    assert prompt_library.resolve_version("chat_system_rag") == "v1"
    assert prompt_library.resolve_version("chat_system_rag", "v3-lean") == "v3-lean"

    monkeypatch.setattr(settings, spec.setting, "", raising=False)
    assert prompt_library.resolve_version("chat_system_rag") == spec.default_version


def test_shipped_config_leaves_every_version_setting_empty():
    """出厂配置必须让第三层可达。

    这些配置项是**覆盖项**,默认版本的唯一事实来源是 SPECS.default_version。
    在 config.py 里写死具体版本号会让 resolve_version 的第三层对所有带 setting
    的 key 永远走不到（配置非空就总是赢）,于是同一个版本号在两个文件里各写一遍,
    改一处不改另一处不会有任何报错。

    读 model_fields 里的类默认值,不读 ``Settings()`` 也不读全局 ``settings``：
    那两个都会把本机 .env 加载进来,而 .env 里填了什么是用户的选择,不是出厂默认。
    这条差别本身就踩过一次——Settings() 在有 .env 的机器上根本测不出这个问题。
    """
    for key, spec in prompt_library.SPECS.items():
        if not spec.setting:
            continue
        assert Settings.model_fields[spec.setting].default == "", (
            f"{spec.setting} 在 config.py 里写死了版本号，"
            f"prompts/{key} 的 default_version 会永远走不到"
        )


def test_default_version_is_reachable_without_any_env(monkeypatch):
    """把所有版本配置清空（模拟一台没有 .env 的机器），每个 key 都应当解析到
    自己契约里的默认版本。

    必须 monkeypatch 全局 settings：resolve_version 读的是它，而本机 .env
    可能已经覆盖过某几项。
    """
    for spec in prompt_library.SPECS.values():
        if spec.setting:
            monkeypatch.setattr(settings, spec.setting, "", raising=False)

    for key, spec in prompt_library.SPECS.items():
        assert prompt_library.resolve_version(key) == spec.default_version


def test_ref_is_key_at_version():
    template = prompt_library.get("chat_system_rag", "v2")
    assert template.ref == "chat_system_rag@v2"


def test_catalog_marks_exactly_one_active_version_per_key():
    for entry in prompt_library.catalog():
        active = [v for v in entry["versions"] if v["isActive"]]
        assert len(active) == 1, entry["key"]
        assert active[0]["version"] == entry["activeVersion"]


def test_delegation_versions_declare_their_mode():
    """启动校验靠 expects 判断"这一版是为哪种模式写的"，声明漏了就等于放行。"""
    augment = prompt_library.get("chat_system_rag", "v5-augment")
    supervisor = prompt_library.get("chat_system_rag", "v6-supervisor")

    assert augment.expects_all("delegation")
    assert "supervisor" not in augment.expects, "augment 版不能声明 supervisor"
    assert supervisor.expects_all("delegation", "supervisor")


def test_supervisor_version_does_not_promise_tools_it_lacks():
    """supervisor 模式下检索类工具已被角色收走，正文不能再把它们列成自己的能力。

    这正是旧 v5-supervisor 被弃用的原因：它照抄了 v4 的工具清单。
    """
    body = prompt_library.get("chat_system_rag", "v6-supervisor").body

    assert "search_knowledge_base" not in body
    assert "web_search" not in body
    assert "delegate" in body


def test_deprecated_supervisor_version_is_archived():
    """名字与内容不符的那一版必须是 archived，否则还会被人配上去。"""
    assert prompt_library.get("chat_system_rag", "v5-supervisor").status == "archived"


def test_workspace_versions_declare_workspace_tools():
    for version in ("v4-workspace", "v5-augment", "v6-supervisor"):
        template = prompt_library.get("chat_system_rag", version)
        assert "workspace-tools" in template.expects, version


def test_unknown_expects_value_is_rejected():
    """拼错的 expects 静默忽略，等于启动校验以为这一版没有前置条件。"""
    with pytest.raises(PromptError):
        prompt_library._parse(
            "---\nstatus: candidate\nexpects: delegatoin\n---\n正文",
            "chat_system_rag",
            "vtest",
        )


def test_expects_defaults_to_empty_and_parses_list():
    plain = prompt_library._parse(
        "---\nstatus: active\n---\n正文", "chat_system_plain", "vtest"
    )
    assert plain.expects == ()

    multi = prompt_library._parse(
        "---\nstatus: candidate\nexpects: workspace-tools, delegation\n---\n正文",
        "chat_system_rag",
        "vtest",
    )
    assert multi.expects == ("workspace-tools", "delegation")
    assert multi.expects_all("delegation", "workspace-tools")
    assert not multi.expects_all("supervisor")


def test_catalog_exposes_expects():
    entry = next(e for e in prompt_library.catalog() if e["key"] == "chat_system_rag")
    versions = {v["version"]: v for v in entry["versions"]}
    assert "delegation" in versions["v5-augment"]["expects"]
    assert versions["v2"]["expects"] == []


def test_lean_version_is_actually_shorter():
    """v3-lean 的整个卖点是省 token；如果它比 v2 还长，那它就没有存在理由。"""
    v2 = prompt_library.get("chat_system_rag", "v2")
    lean = prompt_library.get("chat_system_rag", "v3-lean")
    assert len(lean.body) < len(v2.body)


# ========== 解析器本身的边界 ==========


def _parse(body: str, key: str = "chat_system_plain"):
    return prompt_library._parse(f"---\nstatus: active\n---\n{body}", key, "vtest")


def test_unbalanced_condition_block_is_rejected():
    with pytest.raises(PromptError):
        _parse("[[if prefetched]]\n只开了不关\n", key="chat_system_rag")


def test_orphan_endif_is_rejected():
    with pytest.raises(PromptError):
        _parse("正文\n[[endif]]\n", key="chat_system_rag")


def test_undeclared_flag_in_template_is_rejected():
    """模板里写了 SPECS 没声明的开关，通常是拼写错误，渲染时它永远为假。"""
    with pytest.raises(PromptError):
        _parse("[[if prefetchd]]\n错字\n[[endif]]\n", key="chat_system_rag")


def test_extra_placeholder_is_rejected():
    with pytest.raises(PromptError):
        _parse("你好 {nickname}")


def test_missing_required_placeholder_is_rejected():
    with pytest.raises(PromptError):
        _parse("只有问题没有参考内容：{question}", key="eval_rag_answer")


def test_missing_frontmatter_is_rejected():
    with pytest.raises(PromptError):
        prompt_library._parse("没有元数据块的正文", "chat_system_plain", "vtest")


def test_invalid_status_is_rejected():
    with pytest.raises(PromptError):
        prompt_library._parse(
            "---\nstatus: 差不多能用\n---\n正文", "chat_system_plain", "vtest"
        )


def test_escaped_braces_survive_rendering():
    """模板里放一段 JSON 示例不该被当成占位符。"""
    template = prompt_library._parse(
        '---\nstatus: active\n---\n输出 {{"ok": true}}',
        "chat_system_plain",
        "vtest",
    )
    assert template.placeholders == ()
    assert template.render() == '输出 {"ok": true}'
