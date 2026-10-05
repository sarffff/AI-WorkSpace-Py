"""跨动作的防篡改审计链（配合 models.AuditLog）。

与 ``approval_audit`` 分工：那个模块只提供参数摘要算法（digest/preview，审批、
账本与工单轨迹共用同一个哈希口径）；这里记"发生了哪个改变状态的动作"，并串成一条
**哈希链**，用来回答"事后有没有被改过"。

## 防篡改怎么成立

每条存上一条的 ``entry_hash`` 作 ``prev_hash``，自己的 ``entry_hash`` =
sha256(prev_hash + actor + seq + action + target + args_digest + ts)。删除或改动
中间任意一条，它之后每条都对不上——``verify`` 从头走一遍即可定位断点。链**按
actor 分段**：``seq`` 是该 actor 内从 1 起的序号。

## 三条诚实边界

1. **并发追加 TOCTOU。** record 要先读该 actor 最后一条拿 prev_hash 与 seq，两次
   并发会读到同一 prev_hash（结构上消不掉，只能标出来）；真撞上链会分叉、verify
   报断点。审计动作低频（审批/写工具/登录），实际极难撞上。
2. **失败只记日志、不阻断。** 审计是记录不是闸门；让审计写失败挡住用户的写操作，
   等于把可观测性变成单点故障（同 approval_audit / ticket_outbox）。
3. **ts 抹掉微秒。** entry_hash 覆盖时间戳，而 MySQL 的 DATETIME 不存小数秒——
   存进再读出微秒会丢、verify 就对不上。record 时先清零，让算 hash 的值与落库
   读回的值逐位一致。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from models import AuditLog
from services import approval_audit
from services.clock import naive_now

logger = logging.getLogger("audit_log")

# 首条的 prev_hash；全零串一眼认出"链的起点"
GENESIS_HASH = "0" * 64

def _norm(value: Any) -> str:
    return "" if value is None else str(value)


def _entry_hash(
    *,
    prev_hash: str,
    actor_id: str,
    seq: int,
    action: str,
    target: str | None,
    args_digest: str | None,
    ts_iso: str,
) -> str:
    """一条 entry 的哈希。纯函数——record 与 verify 必须算得逐位一致。"""
    payload = "\n".join(
        [prev_hash, actor_id, str(seq), action, _norm(target), _norm(args_digest), ts_iso]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest(db: Session, actor_id: str) -> AuditLog | None:
    return (
        db.query(AuditLog)
        .filter(AuditLog.actor_id == actor_id)
        .order_by(AuditLog.seq.desc())
        .first()
    )

def record(
    db: Session,
    *,
    actor_id: str,
    action: str,
    target: str | None = None,
    arguments: Any = None,
    ticket_id: str | None = None,
) -> str | None:
    """追加一条审计。返回记录 id，失败返回 None（只记日志、不抛）。"""
    try:
        prev = _latest(db, actor_id)
        prev_hash = prev.entry_hash if prev else GENESIS_HASH
        seq = (prev.seq + 1) if prev else 1

        args_digest = approval_audit.digest(arguments) if arguments is not None else None
        args_preview = (
            approval_audit.preview(arguments) if arguments is not None else None
        )
        ts = naive_now().replace(microsecond=0)  # 微秒清零，见模块文档
        entry_hash = _entry_hash(
            prev_hash=prev_hash,
            actor_id=actor_id,
            seq=seq,
            action=action,
            target=target,
            args_digest=args_digest,
            ts_iso=ts.isoformat(),
        )
        entry = AuditLog(
            actor_id=actor_id,
            seq=seq,
            action=action[:40],
            target=(target[:255] if target else None),
            args_digest=args_digest,
            args_preview=args_preview,
            ticket_id=ticket_id,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            created_at=ts,
        )
        db.add(entry)
        db.commit()
        return entry.id
    except Exception:
        db.rollback()
        logger.exception(
            "failed to record audit entry action=%s actor=%s", action, actor_id
        )
        return None

def _row_to_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "seq": row.seq,
        "action": row.action,
        "target": row.target,
        "argumentsPreview": row.args_preview,
        "argumentsDigest": row.args_digest,
        "ticketId": row.ticket_id,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def history(
    db: Session, actor_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """某个 actor 的审计条目，按 seq 倒序（新的在前）。给自作用域的读接口。"""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.actor_id == actor_id)
        .order_by(AuditLog.seq.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
        .all()
    )
    return [_row_to_dict(row) for row in rows]


def verify(db: Session, actor_id: str) -> dict[str, Any]:
    """从头走一遍该 actor 的链，返回完整性。

    ``firstBrokenSeq`` 非空即那一条起链接对不上——它或它之前被改过/删过。
    """
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.actor_id == actor_id)
        .order_by(AuditLog.seq.asc())
        .all()
    )
    expected_prev = GENESIS_HASH
    for row in rows:
        recomputed = _entry_hash(
            prev_hash=row.prev_hash,
            actor_id=row.actor_id,
            seq=row.seq,
            action=row.action,
            target=row.target,
            args_digest=row.args_digest,
            ts_iso=row.created_at.isoformat() if row.created_at else "",
        )
        if row.prev_hash != expected_prev or recomputed != row.entry_hash:
            return {"ok": False, "entries": len(rows), "firstBrokenSeq": row.seq}
        expected_prev = row.entry_hash
    return {"ok": True, "entries": len(rows), "firstBrokenSeq": None}


__all__ = ["GENESIS_HASH", "history", "record", "verify"]
