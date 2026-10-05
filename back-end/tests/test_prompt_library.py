"""提示词注册表的测试。

重点不在"渲染出来的字符串等于什么"——那是模板文件的内容，会随时被改。
重点在**契约**：占位符对不上、条件段没闭合、版本不存在、expects 拼错时，必须炸在
加载/渲染阶段，而不是让一段自洽但错误的提示词进到模型那边。

键名一律取现在真实存在的 spec：``ticket_agent``（无占位符、无开关，最适合当解析器
的空白靶）、``ticket_understand``（有 required 占位符）、``ticket_sub_inquiry``
（有 setting 且有 v1/v2 两版，是唯一能测"版本切换"路径的）。
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


def test_condition_markers_never_leak():
    for key in prompt_library.SPECS:
        for template in prompt_library.versions(key):
            flags = {name: True for name in template.flags}
            values = {name: "X" for name in template.placeholders}
            rendered = template.render(flags=flags or None, **values)
            assert "[[" not in rendered


def test_unknown_flag_raises_instead_of_being_ignored():
    """静默忽略未声明的开关，等于换版本时漏改调用方也没人知道。"""
    template = prompt_library.get("ticket_agent")  # 这一版不声明任何开关
    with pytest.raises(PromptError):
        template.render(flags={"prefetched": True})


def test_missing_placeholder_value_raises():
    template = prompt_library.get("eval_rag_answer", "v1")
    with pytest.raises(PromptError):
        template.render(context="只给了一半")


def test_unknown_version_raises_with_available_list():
    with pytest.raises(PromptError) as excinfo:
        prompt_library.get("ticket_agent", "v99")
    assert "v99" in str(excinfo.value)


def test_unknown_key_raises():
    with pytest.raises(PromptError):
        prompt_library.get("no_such_prompt")


def test_subagent_prompts_are_switchable():
    """三个角色的版本必须能按配置切,否则 prompt_key 那套机制是空转的。

    没有 setting 时 resolve_version 只剩 default_version 一个出口——新写一版
    进去没有任何代码路径能到达它。
    """
    for key in ("ticket_sub_inquiry", "ticket_sub_policy", "ticket_sub_reassurance"):
        spec = prompt_library.SPECS[key]
        assert spec.setting, f"{key} 不可切版本"
        assert hasattr(settings, spec.setting), f"{spec.setting} 没有对应的配置项"


def test_each_role_has_its_own_setting():
    """共享一个开关就没法单独 A/B 某个角色,动一个会让另两个的结果一起失效。"""
    settings_used = [
        prompt_library.SPECS[key].setting
        for key in ("ticket_sub_inquiry", "ticket_sub_policy", "ticket_sub_reassurance")
    ]
    assert len(set(settings_used)) == 3, settings_used


def test_role_prompt_follows_its_setting(monkeypatch):
    """改配置要真的换到另一版——这是 ``role_prompt()`` 唯一的切换入口。"""
    from services import agent_roles, subagent

    role = agent_roles.ROLES["policy"]
    spec = prompt_library.SPECS[role.prompt_key]

    # 指向一个不存在的版本：报错说明 setting 真的被读了（而不是静默用默认版）
    monkeypatch.setattr(settings, spec.setting, "v-nope", raising=False)
    with pytest.raises(PromptError):
        subagent.role_prompt(role)

    monkeypatch.setattr(settings, spec.setting, "", raising=False)
    assert subagent.role_prompt(role) == prompt_library.get(role.prompt_key, "v1").body


def test_version_resolution_order(monkeypatch):
    """显式传参 > settings > 契约默认值。"""
    spec = prompt_library.SPECS["ticket_sub_inquiry"]
    monkeypatch.setattr(settings, spec.setting, "v1", raising=False)
    assert prompt_library.resolve_version("ticket_sub_inquiry") == "v1"
    assert prompt_library.resolve_version("ticket_sub_inquiry", spec.default_version) == (
        spec.default_version
    )

    monkeypatch.setattr(settings, spec.setting, "", raising=False)
    assert prompt_library.resolve_version("ticket_sub_inquiry") == spec.default_version


def test_shipped_config_leaves_every_version_setting_empty():
    """出厂配置必须让第三层可达。

    这些配置项是**覆盖项**,默认版本的唯一事实来源是 SPECS.default_version。
    在 config.py 里写死具体版本号会让 resolve_version 的第三层对所有带 setting
    的 key 永远走不到（配置非空就总是赢）,于是同一个版本号在两个文件里各写一遍,
    改一处不改另一处不会有任何报错。

    读 model_fields 里的类默认值,不读 ``Settings()`` 也不读全局 ``settings``：
    那两个都会把本机 .env 加载进来,而 .env 里填了什么是用户的选择,不是出厂默认。
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
    template = prompt_library.get("ticket_sub_inquiry")
    assert template.ref == f"ticket_sub_inquiry@{template.version}"


def test_catalog_marks_exactly_one_active_version_per_key():
    for entry in prompt_library.catalog():
        active = [v for v in entry["versions"] if v["isActive"]]
        assert len(active) == 1, entry["key"]
        assert active[0]["version"] == entry["activeVersion"]


# ========== 解析器本身的边界 ==========


def _parse(body: str, key: str = "ticket_agent"):
    return prompt_library._parse(f"---\nstatus: active\n---\n{body}", key, "vtest")


def test_unbalanced_condition_block_is_rejected():
    with pytest.raises(PromptError):
        _parse("[[if prefetched]]\n只开了不关\n")


def test_orphan_endif_is_rejected():
    with pytest.raises(PromptError):
        _parse("正文\n[[endif]]\n")


def test_undeclared_flag_in_template_is_rejected():
    """模板里写了 SPECS 没声明的开关，通常是拼写错误，渲染时它永远为假。"""
    with pytest.raises(PromptError):
        _parse("[[if prefetchd]]\n错字\n[[endif]]\n")


def test_extra_placeholder_is_rejected():
    with pytest.raises(PromptError):
        _parse("你好 {nickname}")


def test_missing_required_placeholder_is_rejected():
    with pytest.raises(PromptError):
        _parse("只有问题没有工单正文：{question}", key="ticket_understand")


def test_missing_frontmatter_is_rejected():
    with pytest.raises(PromptError):
        prompt_library._parse("没有元数据块的正文", "ticket_agent", "vtest")


def test_invalid_status_is_rejected():
    with pytest.raises(PromptError):
        prompt_library._parse(
            "---\nstatus: 差不多能用\n---\n正文", "ticket_agent", "vtest"
        )


def test_escaped_braces_survive_rendering():
    """模板里放一段 JSON 示例不该被当成占位符。"""
    template = prompt_library._parse(
        '---\nstatus: active\n---\n输出 {{"ok": true}}',
        "ticket_agent",
        "vtest",
    )
    assert template.placeholders == ()
    assert template.render() == '输出 {"ok": true}'
