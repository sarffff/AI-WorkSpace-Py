"""security_preflight 的测试：audit_settings 纯函数 + enforce 的生产/dev 行为。

纯函数、不触库——用一个 SimpleNamespace 冒充 settings，各不安全值单独验。
"""
from __future__ import annotations

import logging
import types

import pytest

from services import security_preflight


def _settings(**over):
    base = dict(
        JWT_SECRET_KEY="x" * 40,  # 够长的真 key
        LLM_API_KEY="real-key",
        DATABASE_URL="mysql+pymysql://app:s3cret@db:3306/x",
        VECTOR_STORE="memory",
        QDRANT_API_KEY="",
        QDRANT_URL="http://localhost:6333",
        ENV="dev",
    )
    base.update(over)
    ns = types.SimpleNamespace(**base)
    ns.is_production = ns.ENV == "production"
    return ns


def test_clean_config_has_no_issues():
    assert security_preflight.audit_settings(_settings()) == []


def test_jwt_placeholder_prefix_caught():
    # _DEFAULT_JWT_KEY 与 .env.example 带后缀的值前缀一致，都该命中（前缀检查正是
    # 现有那道精确相等检查漏掉 .env.example 值的修法）
    for key in (
        "your-secret-key-change-this-in-production",
        "your-secret-key-change-this-in-production-please-make-it-very-long-and-random",
    ):
        issues = security_preflight.audit_settings(_settings(JWT_SECRET_KEY=key))
        assert any("JWT_SECRET_KEY" in i for i in issues)


def test_short_jwt_caught():
    issues = security_preflight.audit_settings(_settings(JWT_SECRET_KEY="short"))
    assert any("过短" in i for i in issues)


def test_llm_and_db_placeholders_caught():
    issues = security_preflight.audit_settings(
        _settings(
            LLM_API_KEY="your_api_key_here",
            DATABASE_URL="mysql+pymysql://root:password@localhost/x",
        )
    )
    assert any("LLM_API_KEY" in i for i in issues)
    assert any("DATABASE_URL" in i for i in issues)


def test_qdrant_unauth_only_when_exposed():
    # 本机 qdrant 无 key → 不算问题
    assert (
        security_preflight.audit_settings(
            _settings(VECTOR_STORE="qdrant", QDRANT_URL="http://localhost:6333")
        )
        == []
    )
    # 非本机 qdrant 无 key → 算问题
    issues = security_preflight.audit_settings(
        _settings(VECTOR_STORE="qdrant", QDRANT_URL="http://vec.prod:6333")
    )
    assert any("Qdrant" in i for i in issues)
    # 非本机但配了 key → 不算
    assert (
        security_preflight.audit_settings(
            _settings(
                VECTOR_STORE="qdrant", QDRANT_URL="http://vec.prod:6333", QDRANT_API_KEY="k"
            )
        )
        == []
    )


def test_enforce_raises_in_production_warns_in_dev():
    placeholder = "your-secret-key-change-this-in-production"
    with pytest.raises(RuntimeError):
        security_preflight.enforce(
            _settings(JWT_SECRET_KEY=placeholder, ENV="production"),
            logger=logging.getLogger("t"),
        )
    # dev：只告警、不抛（不抛即通过）
    security_preflight.enforce(
        _settings(JWT_SECRET_KEY=placeholder, ENV="dev"),
        logger=logging.getLogger("t"),
    )


# ========== 写操作与审批闸门的联动 ==========


def test_write_gate_flags_ungated_state_changing_tools():
    """写工具注册了却不在门集里 = 没有人确认就写得进去。只读工具不该被牵连。"""
    issues = security_preflight.audit_write_gates(
        _settings(),
        registered={"save_to_knowledge_base", "delete_file", "read_file", "search_files"},
        gated=set(),
    )
    assert len(issues) == 1
    assert "save_to_knowledge_base" in issues[0]
    assert "delete_file" in issues[0]
    # 只读工具不在 STATE_CHANGING_TOOLS 里，出现就说明集合算错了
    assert "read_file" not in issues[0] and "search_files" not in issues[0]


def test_write_gate_silent_when_everything_gated_or_absent():
    state_changing = {"save_to_knowledge_base", "delete_knowledge_document",
                      "write_file", "edit_file", "delete_file"}
    # 全部门禁打开 → 没问题
    assert security_preflight.audit_write_gates(_settings(), registered=state_changing, gated=state_changing) == []
    # 根本没注册写工具（默认配置）→ 也没问题，哪怕审批是关的
    assert security_preflight.audit_write_gates(_settings(), registered={"read_file"}, gated=set()) == []


def test_write_gate_catches_listed_mode_that_excludes_writes():
    """``listed`` 模式把写工具漏在外面，和 mode=off 一样是洞。"""
    issues = security_preflight.audit_write_gates(
        _settings(),
        registered={"write_file", "edit_file"},
        gated={"web_search"},  # 给检索加了审批，反而没 gate 写
    )
    assert issues and "write_file" in issues[0]


def test_enforce_warns_when_write_enabled_without_approval(monkeypatch, caplog):
    """真接上注册代码走一遍：开写工具、审批保持默认 off → dev 必须告警。

    这条测的是"审计取的输入确实来自注册那一段"，而不只是集合运算本身。
    """
    from config import settings as live

    monkeypatch.setattr(live, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(live, "TOOL_FS_WRITE_ENABLED", True)
    monkeypatch.setattr(live, "AGENT_APPROVAL_MODE", "off")
    monkeypatch.setattr(live, "AGENT_CHECKPOINT_ENABLED", False)

    logger = logging.getLogger("preflight.writegate")
    with caplog.at_level(logging.WARNING, logger="preflight.writegate"):
        security_preflight.enforce(_settings(ENV="dev"), logger=logger)

    assert any("write_file" in r.message for r in caplog.records)


def test_enforce_refuses_in_production(monkeypatch):
    from config import settings as live

    monkeypatch.setattr(live, "TOOL_WRITE_KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(live, "AGENT_APPROVAL_MODE", "off")
    monkeypatch.setattr(live, "AGENT_CHECKPOINT_ENABLED", False)

    with pytest.raises(RuntimeError, match="save_to_knowledge_base"):
        security_preflight.enforce(_settings(ENV="production"), logger=logging.getLogger("t"))


def test_state_changing_list_covers_every_write_flag(monkeypatch):
    """新加写工具却忘了进 STATE_CHANGING_TOOLS → 上面那条审计会静默放过。

    洞不是"集合算错"，是"没人把新工具加进来"，所以把四个开关全打开对一遍：
    凡注册出来的改状态工具都必须在门集清单里出现。
    """
    from config import settings as live
    from services import fs_tools, workspace_tools
    from services.approval import STATE_CHANGING_TOOLS

    for name in (
        "TOOL_FS_ENABLED",
        "TOOL_FS_WRITE_ENABLED",
        "TOOL_FS_DELETE_ENABLED",
        "TOOL_WRITE_KNOWLEDGE_ENABLED",
        "TOOL_DELETE_KNOWLEDGE_ENABLED",
    ):
        monkeypatch.setattr(live, name, True)

    registered = set(fs_tools.enabled_names()) | set(workspace_tools.enabled_names())
    writes = {
        "save_to_knowledge_base",
        "delete_knowledge_document",
        "write_file",
        "edit_file",
        "delete_file",
    }

    assert writes <= registered, f"开关与注册脱钩了：{writes - registered}"
    assert writes <= set(
        STATE_CHANGING_TOOLS
    ), "有写工具没进 STATE_CHANGING_TOOLS，启动审计会漏掉它"
