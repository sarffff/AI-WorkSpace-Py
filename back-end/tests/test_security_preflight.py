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
