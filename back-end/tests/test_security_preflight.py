"""security_preflight 的测试：audit_settings / audit_refund_limits 纯函数 + enforce 的环境行为。

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
        # 默认不开 Agent：这样"干净配置"那条测的确实是默认状态
        TICKET_AGENT_ENABLED=False,
        TICKET_DAILY_REFUND_LIMIT=0,
    )
    base.update(over)
    ns = types.SimpleNamespace(**base)
    ns.is_production = ns.ENV == "production"
    return ns


class _RecordingLogger:
    """替 caplog 收 warning——这里要断言的是"告警文案里有没有那个配置名"。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message, *args) -> None:
        self.warnings.append(message % args if args else str(message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


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


# ========== 治理护栏：开着 Agent 却没有资金上限 ==========


def test_agent_关掉时不要求退款上限():
    """没有资金通道的时候，不设上限是正确配置而不是漏洞。"""
    assert security_preflight.audit_refund_limits(_settings()) == []


def test_开着_agent_却没有当日退款上限时报一条():
    issues = security_preflight.audit_refund_limits(
        _settings(TICKET_AGENT_ENABLED=True, TICKET_DAILY_REFUND_LIMIT=0)
    )
    assert len(issues) == 1
    assert "TICKET_DAILY_REFUND_LIMIT" in issues[0]


def test_上限为正数时通过():
    assert security_preflight.audit_refund_limits(
        _settings(TICKET_AGENT_ENABLED=True, TICKET_DAILY_REFUND_LIMIT=5000)
    ) == []


def test_enforce_在_dev_告警在生产拒绝():
    """同一条缺护栏，两种环境两种处置——判据只写一遍。"""
    logger = _RecordingLogger()
    security_preflight.enforce(
        _settings(ENV="dev", TICKET_AGENT_ENABLED=True, TICKET_DAILY_REFUND_LIMIT=0),
        logger=logger,
    )
    assert any("TICKET_DAILY_REFUND_LIMIT" in m for m in logger.warnings)

    with pytest.raises(RuntimeError, match="TICKET_DAILY_REFUND_LIMIT"):
        security_preflight.enforce(
            _settings(
                ENV="production", TICKET_AGENT_ENABLED=True, TICKET_DAILY_REFUND_LIMIT=0
            ),
            logger=logging.getLogger("t"),
        )
