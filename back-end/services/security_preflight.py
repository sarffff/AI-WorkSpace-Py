"""启动时的"安全默认值"体检（GAPS.md C4）。

交付前最该拦的一类事故：拿着示例配置（占位密钥、示例库口令、无鉴权的向量库）就上了
生产。config.py 底部已有一道 JWT fail-fast，但它只精确等于那一个占位串——shipped
.env.example 的 JWT 值带了后缀就能绕过，且不管 LLM/DB/Qdrant。

这里把判据集中成一个**纯函数** audit_settings()，返回问题清单（空=通过）。main.py
启动时调用：**生产**(ENV=production) 有问题即 RuntimeError 拒绝启动；**非生产**只打
warning（dev 要能用示例值把项目跑起来，但得看见提醒）。

为什么是启动闸门而不是提示词/文档：示例值能跑通是最隐蔽的失败——它不报错，只是把
一个公开已知的密钥、一个无鉴权的知识库向量端点带上了线。约束要落在启动闸门上，
不是 README 里一句"记得改"。
"""
from __future__ import annotations

# JWT 占位值的公共前缀：既盖住 config._DEFAULT_JWT_KEY
# ("your-secret-key-change-this-in-production")，也盖住 .env.example 里带后缀的那个
# ("...-please-make-it-very-long-and-random")。用前缀而非精确相等，正是现有那道检查
# 漏掉 .env.example 值的原因。
_JWT_PLACEHOLDER_PREFIX = "your-secret-key-change-this"
_JWT_MIN_LENGTH = 32

_LLM_KEY_PLACEHOLDER = "your_api_key_here"
_DB_EXAMPLE_CRED = "root:password@"


def audit_settings(settings) -> list[str]:
    """返回所有"不安全默认值"问题（空列表 = 通过）。纯函数，便于单测。"""
    issues: list[str] = []

    key = settings.JWT_SECRET_KEY or ""
    if key.startswith(_JWT_PLACEHOLDER_PREFIX):
        issues.append("JWT_SECRET_KEY 仍是占位/示例值（.env.example 原样），必须换成随机长串")
    elif len(key) < _JWT_MIN_LENGTH:
        issues.append(
            f"JWT_SECRET_KEY 过短（{len(key)}<{_JWT_MIN_LENGTH} 字符），易被暴力破解"
        )

    if (settings.LLM_API_KEY or "") in ("", _LLM_KEY_PLACEHOLDER):
        issues.append("LLM_API_KEY 仍是占位值或为空")

    if _DB_EXAMPLE_CRED in (settings.DATABASE_URL or ""):
        issues.append("DATABASE_URL 仍是示例账号口令 root:password")

    # Qdrant 默认无鉴权。只在"用 qdrant 且开在非本机地址且没配 key"时算问题——
    # 本机 memory 后端、或本机 qdrant 不对外暴露，就不是这条要拦的事。
    if settings.VECTOR_STORE == "qdrant" and not (settings.QDRANT_API_KEY or ""):
        url = settings.QDRANT_URL or ""
        if "localhost" not in url and "127.0.0.1" not in url:
            issues.append(
                "Qdrant 开在非本机地址却未设 QDRANT_API_KEY，知识库向量将无鉴权暴露"
            )

    return issues


def audit_write_gates(settings, registered: set[str], gated: set[str]) -> list[str]:
    """改状态的工具已经注册，却不在审批门集里 = 没有人确认就写得进去。

    判据不在这儿重复一遍：``registered`` 由 ``fs_tools.enabled_names()`` 与
    ``workspace_tools.enabled_names()`` 给（它们就是注册那一段的同一份代码），
    ``gated`` 由 ``approval.gated_tools()`` 给。这里只做集合差。之所以不让本函数
    自己去调那两个模块，是因为 ``audit_*`` 都是纯函数——启动闸门要能被单测喂进
    任意组合，而不必真把工具注册起来。

    三个条件叠在一起才会漏，而它们各自看起来都"没什么"：
    ``AGENT_APPROVAL_MODE="off"``（默认）+ ``AGENT_CHECKPOINT_ENABLED=False``
    （默认）+ 有人把 ``TOOL_WRITE_KNOWLEDGE_ENABLED`` 之类打开了。前两个默认值是
    成套的（都关，所以默认配置没问题），坏在第三个是逐个打开的：运营按
    `standard.md` §8"写操作要人确认"的理解去开写工具时，并不会同时得到审批。
    """
    # 延迟 import：审计函数要能在没装齐依赖、或测试喂假 settings 的情况下被调用，
    # 而这个模块被 main.py 在启动很早期就 import。
    from services.approval import STATE_CHANGING_TOOLS

    ungated = sorted(registered & set(STATE_CHANGING_TOOLS) - gated)
    if not ungated:
        return []
    return [
        "已注册但不过审批的改状态工具："
        + "、".join(ungated)
        + "——写操作会在没有人确认的情况下直接执行。"
        "要么关掉对应 TOOL_*_ENABLED，要么同时开 "
        "AGENT_APPROVAL_MODE=write 与 AGENT_CHECKPOINT_ENABLED=True"
        "（审批要跨请求等人点同意，没有快照就没有东西可恢复，两个都得开）"
    ]


def _live_tool_names() -> tuple[set[str], set[str]]:
    """当前配置下"注册了哪些工具"与"哪些要审批"。给 enforce 用，不参与纯函数审计。"""
    from services import fs_tools, workspace_tools
    from services.approval import gated_tools

    registered = set(fs_tools.enabled_names()) | set(workspace_tools.enabled_names())
    return registered, set(gated_tools())


def enforce(settings, *, logger) -> None:
    """启动体检。生产有问题即抛 RuntimeError 拒绝启动；非生产只告警。"""
    issues = list(audit_settings(settings))
    try:
        registered, gated = _live_tool_names()
    except Exception as exc:  # 工具面取不到不该挡住启动，但也别装作查过了
        logger.warning("[写操作审批] 无法核对工具面（%s），这一项没检查", type(exc).__name__)
    else:
        issues += audit_write_gates(settings, registered, gated)

    if not issues:
        return
    if settings.is_production:
        raise RuntimeError(
            "检测到不安全的默认配置，生产环境拒绝启动：\n- " + "\n- ".join(issues)
        )
    for issue in issues:
        logger.warning("[安全默认值] %s（dev 放行，生产会拒绝启动）", issue)
