"""审批的审计线索：谁、什么时候、批准的是哪一版参数。

与 ``approval.py`` 分开：那个模块管**判定**（哪些工具要审批、参数改动是否合法、
回灌给模型的说明怎么写），是纯函数、不碰数据库。这个模块管**留痕**，只写库。
混在一起会让 ``approval`` 从一个可以随便调的纯模块变成需要 Session 的东西。

## 摘要而不是完整参数

``save_to_knowledge_base`` 的参数里是整篇文档正文（上限 ``AGENT_WRITE_MAX_CHARS``，
默认 20000 字符）。整份存进审计表等于同一份用户内容在库里存两遍，而审计要回答的
问题是"当时批准的到底是不是这一份"——SHA-256 足以证明同一性，预览足以让人认出
是哪一份。

摘要算在**序列化时按 key 排序**的 JSON 上，和 ``RepeatGuard.key`` 同一个理由：
``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 是同一份参数，不排序会让同一份参数算出
两个不同的 digest，于是"批准的和执行的是同一份"这个判断会假阴性。

## 失败一律只记日志

审计写入失败**不能**让审批流程中断。这是个记录，不是闸门——闸门是
``approval.requires_approval``。让一次审计表写入失败把用户的写操作也一起挡掉，
等于把可观测性变成了单点故障。同 ``checkpoint_store`` 的取舍。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from models import AgentApproval
from services.clock import naive_now

logger = logging.getLogger("approval_audit")

# 预览的截断长度。够认出"是哪一份"，不够复制一整篇文档
_PREVIEW_MAX_CHARS = 500


def digest(arguments: Any) -> str:
    """参数的 SHA-256。按 key 排序后序列化，见模块文档。"""
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview(arguments: Any) -> str:
    """给人看的参数预览，截断到 ``_PREVIEW_MAX_CHARS``。

    截断标记写出实际长度而不是只写省略号：审计时"这份参数原本有多长"本身
    就是信息——20000 字符的写入和 200 字符的写入是两件不同的事。
    """
    try:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    return f"{text[:_PREVIEW_MAX_CHARS]}…（共 {len(text)} 字符）"


def record_request(
    db: Session,
    *,
    run_id: str,
    chat_id: str | None,
    user_id: str,
    tool_name: str,
    tool_call_id: str | None,
    round_index: int,
    call_index: int,
    arguments: dict[str, Any],
    reason: str = "",
) -> str | None:
    """登记一次"等待审批"。返回记录 id，失败返回 None。

    在**发出 approval_required 之前**调用，与快照落库同一个位置：如果先把事件
    发给前端、再写审计，那么进程在这两步之间挂掉就会留下一个用户看到过、
    但审计里不存在的审批请求。
    """
    try:
        record = AgentApproval(
            id=str(uuid.uuid4()),
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            round_index=round_index,
            call_index=call_index,
            arguments_digest=digest(arguments),
            arguments_preview=preview(arguments),
            decision="pending",
            requested_at=naive_now(),
            reason=(reason or "")[:255] or None,
        )
        db.add(record)
        db.commit()
        return record.id
    except Exception:
        db.rollback()
        logger.exception("failed to record approval request for run %s", run_id)
        return None


def record_decision(
    db: Session,
    *,
    run_id: str,
    decided_by: str,
    approved: bool,
    note: str = "",
    effective_arguments: dict[str, Any] | None = None,
    edited_fields: list[str] | None = None,
) -> bool:
    """给这个 run 最新那条 pending 记录写上裁决。返回是否写成功。

    **只更新最新那一条 pending**，而不是全部：一次执行可以被打断多次，
    每次裁决只对应一次中断。按 ``requested_at`` 倒序取第一条。

    ``effective_arguments`` 是真正要执行的那一份（用户改过的话就是改后的）。
    它的 digest 与 ``arguments_digest`` 不同就说明执行的不是模型原本要执行的
    东西——那是审计里最值得看的一行。
    """
    try:
        record = (
            db.query(AgentApproval)
            .filter(AgentApproval.run_id == run_id, AgentApproval.decision == "pending")
            .order_by(AgentApproval.requested_at.desc())
            .first()
        )
        if record is None:
            # 审批开关是中途打开的话，历史 run 没有对应的 pending 记录。
            # 这不是错误，但值得留一条日志——否则"审计里少了一条"会被当成 bug 查。
            logger.info("no pending approval record for run %s; decision not audited", run_id)
            return False
        record.decision = "approved" if approved else "rejected"
        record.decided_by = decided_by
        record.decided_at = naive_now()
        record.note = (note or None)
        if effective_arguments is not None:
            record.decided_digest = digest(effective_arguments)
        if edited_fields:
            record.edited_fields = ",".join(edited_fields)[:255]
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("failed to record approval decision for run %s", run_id)
        return False


def expire_stale(db: Session, *, limit: int = 200) -> int:
    """把超时未裁决的 pending 记录标成 ``expired``，返回条数。

    ``AGENT_APPROVAL_TIMEOUT_HOURS`` 在此之前是**死配置**——声明了，全项目引用
    为零。后果有两个：等待审批的 run 永不过期，快照行无限累积；以及一个三天前的
    ``waiting_approval`` 可以现在恢复，而那时工具面会按**当下**的权限重建，
    当初批准的语境早就不存在了。

    ``expired`` 与 ``rejected`` 是两个不同的终态，理由见模型文档。

    这里不起后台调度器：项目里没有 worker 进程，为一件每天发生几次的事引入
    一个新的运行时组件不划算。改成**在列待审批时顺带扫一遍**（读路径本来就要
    查这批数据），加上恢复前单独判一次（那是真正要拦住的地方）。
    """
    hours = settings.AGENT_APPROVAL_TIMEOUT_HOURS
    if hours <= 0:
        return 0
    from datetime import timedelta

    cutoff = naive_now() - timedelta(hours=hours)
    try:
        stale = (
            db.query(AgentApproval)
            .filter(
                AgentApproval.decision == "pending",
                AgentApproval.requested_at < cutoff,
            )
            .limit(limit)
            .all()
        )
        for record in stale:
            record.decision = "expired"
            # decided_by 与 decided_at 保持 NULL：那正是"没有人做这个决定"
        if stale:
            db.commit()
        return len(stale)
    except Exception:
        db.rollback()
        logger.exception("failed to expire stale approvals")
        return 0


def is_expired(requested_at) -> bool:
    """这个请求时间是否已经超过审批时限。

    单独一个纯函数，好让恢复路径在不查审计表的情况下也能判——``agent_runs``
    自己就有 ``updated_at``。
    """
    hours = settings.AGENT_APPROVAL_TIMEOUT_HOURS
    if hours <= 0 or requested_at is None:
        return False
    from datetime import timedelta

    from services.clock import to_naive

    return to_naive(requested_at) < naive_now() - timedelta(hours=hours)


def history(db: Session, run_id: str) -> list[dict[str, Any]]:
    """一次执行的全部审批记录，按时间正序。给审计详情页。"""
    records = (
        db.query(AgentApproval)
        .filter(AgentApproval.run_id == run_id)
        .order_by(AgentApproval.requested_at.asc())
        .all()
    )
    return [
        {
            "id": record.id,
            "tool": record.tool_name,
            "round": record.round_index,
            "decision": record.decision,
            "decidedBy": record.decided_by,
            "decidedAt": record.decided_at.isoformat() if record.decided_at else None,
            "requestedAt": record.requested_at.isoformat() if record.requested_at else None,
            "reason": record.reason,
            "note": record.note,
            "argumentsPreview": record.arguments_preview,
            "argumentsDigest": record.arguments_digest,
            # 执行的和请求的是不是同一份。审计里最值得看的一行
            "argumentsEdited": bool(
                record.decided_digest
                and record.arguments_digest
                and record.decided_digest != record.arguments_digest
            ),
            "editedFields": (
                record.edited_fields.split(",") if record.edited_fields else []
            ),
        }
        for record in records
    ]


__all__ = [
    "digest",
    "expire_stale",
    "history",
    "is_expired",
    "preview",
    "record_decision",
    "record_request",
]
