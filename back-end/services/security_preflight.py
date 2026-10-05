"""启动时的"安全默认值 + 治理护栏"体检。

交付前最该拦的一类事故：拿着示例配置（占位密钥、示例库口令、无鉴权的向量库）就上了
生产。config.py 底部已有一道 JWT fail-fast，但它只精确等于那一个占位串——shipped
.env.example 的 JWT 值带了后缀就能绕过，且不管 LLM/DB/Qdrant。

第二类是**开着 Agent 却没有护栏**：资金类操作没有当日退款上限。这类配置的故障不是
报错，而是账上少了一笔没人记得是谁批的钱，所以它必须和占位密钥同等待遇——在启动时
拦住，而不是写在 README 里。

这里把判据集中成**纯函数**（``audit_settings`` / ``audit_refund_limits``），返回问题
清单（空=通过）。main.py 启动时调用：**生产**(ENV=production) 有问题即 RuntimeError
拒绝启动；**非生产**只打 warning（dev 要能用示例值把项目跑起来，但得看见提醒）。

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


def audit_refund_limits(settings) -> list[str]:
    """工单 Agent 开着，但资金类没有每日上限 = 一夜之间可以把退款金额刷到任意大。

    判据只看配置，不看数据库：这里的定位是"启动时拦住错配置"，而
    ``services/ticket/governor.py`` 才是运行时的闸门（它读当日已退款金额）。
    两边判据不同是为了各自能单独测。
    """
    if not settings.TICKET_AGENT_ENABLED:
        return []
    if int(settings.TICKET_DAILY_REFUND_LIMIT or 0) > 0:
        return []
    return [
        "TICKET_AGENT_ENABLED=true 但 TICKET_DAILY_REFUND_LIMIT 没设正数上限 —— "
        "资金类写操作没有当日熔断。请给一个上限（单位：元）。"
    ]


def enforce(settings, *, logger) -> None:
    """启动体检。生产有问题即抛 RuntimeError 拒绝启动；非生产只告警。"""
    issues = audit_settings(settings) + audit_refund_limits(settings)
    if not issues:
        return
    if settings.is_production:
        raise RuntimeError(
            "检测到不安全的默认配置，生产环境拒绝启动：\n- " + "\n- ".join(issues)
        )
    for issue in issues:
        logger.warning("[安全默认值] %s（dev 放行，生产会拒绝启动）", issue)
