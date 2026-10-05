"""治理层：运行时可改的限额与一键暂停。

对应文档§6 的权限分级与预算熔断。配合 ``models.CsGovernor`` 与
``models.CsOperation``。

**当日已用额度是从 ``cs_operations`` 聚合出来的，不是存在 governor 上的一个计数器**
（为什么这么选，见 CsGovernor 的文档串）。这意味着本模块的每一次判定都要多一次
带索引的 SUM 查询——而它换来的是"限额判定永远不会因为进程崩溃而与实际不一致"。
安全边界上宁可多查一次。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from models import CsGovernor, CsOperation
from services.clock import naive_now

PERMISSIONS = ("read", "mutate", "fund")

# 占用当日资金额度的账本状态。
#
# ``pending_approval`` 也算：人已经点头、只是还没执行，那是一笔已经承诺出去的
# 钱，等它执行完再算额度等于允许同一天里堆二十笔待批退款。
# ``blocked`` 与 ``failed`` 不占——钱没动，也不该因为一次拦截就永久失去额度。
_COUNTED_STATUSES = ("executed", "replayed", "pending_approval")


@dataclass
class Verdict:
    """一次治理判定的结论。``triggers`` 是给人看的那种说法，直接进轨迹。"""

    allowed: bool
    reason: str | None = None
    triggers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "triggers": list(self.triggers),
        }


ALLOWED = Verdict(allowed=True)


def state(db: Session, workspace_id: str) -> CsGovernor | None:
    return (
        db.query(CsGovernor)
        .filter(CsGovernor.workspace_id == workspace_id)
        .first()
    )


def get_or_create(db: Session, workspace_id: str) -> CsGovernor:
    existing = state(db, workspace_id)
    if existing is not None:
        return existing
    row = CsGovernor(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        created_at=naive_now(),
        updated_at=naive_now(),
    )
    db.add(row)
    db.flush()
    return row


def effective_limits(db: Session, workspace_id: str) -> dict[str, Any]:
    """工作区覆盖与全局默认合成为一份。NULL 落到 config 的默认值。

    这里**不建 governor 行**：一个从没被运营改过限额的工作区，读它的限额不该
    顺带写一行数据。
    """
    row = state(db, workspace_id)
    return {
        "paused": bool(row.paused) if row is not None else False,
        "pause_reason": row.pause_reason if row is not None else None,
        "daily_refund_limit": (
            row.daily_refund_limit
            if row is not None and row.daily_refund_limit is not None
            else Decimal(str(settings.TICKET_DAILY_REFUND_LIMIT))
        ),
        "max_cost_per_ticket": (
            row.max_cost_per_ticket
            if row is not None and row.max_cost_per_ticket is not None
            else (
                Decimal(str(settings.TICKET_MAX_COST_PER_TICKET))
                if settings.TICKET_MAX_COST_PER_TICKET
                else None
            )
        ),
        "per_ticket_tool_calls": (
            row.per_ticket_tool_calls
            if row is not None and row.per_ticket_tool_calls is not None
            else int(settings.TICKET_MAX_TOOL_CALLS)
        ),
    }


def day_start(at=None) -> Any:
    """应用时区里"今天零点"。当日额度按自然日而不是滚动 24 小时窗聚合——
    运营说的是"今天最多退多少"，不是"过去 24 小时退了多少"。"""
    moment = at or naive_now()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def refund_used_today(db: Session, workspace_id: str, *, at=None) -> Decimal:
    total = (
        db.query(func.coalesce(func.sum(CsOperation.amount), 0))
        .filter(
            CsOperation.workspace_id == workspace_id,
            CsOperation.permission == "fund",
            CsOperation.status.in_(_COUNTED_STATUSES),
            CsOperation.created_at >= day_start(at),
        )
        .scalar()
    )
    # SQLite 的 SUM 回来的是 float，MySQL 是 Decimal：统一转成 Decimal 再比，
    # 否则 0.1+0.2 那种二进制误差会出现在"到底超没超额度"的判定上
    return Decimal(str(total or 0))


def check_write(
    db: Session,
    workspace_id: str,
    *,
    permission: str,
    amount: Decimal | None = None,
) -> Verdict:
    """写操作执行前的治理判定。查询类不经过这里（read 没有可熔断的代价）。

    只判两件事：**暂停了没有**、**当日资金额度还够不够**。权限档位与风险等级的
    匹配是编排层的事（它才知道这张工单被判成了什么），这里不重复决定一遍。

    ``amount is None`` 在资金类里说的是"这个动作不动钱"，不是"金额没测准"——
    取消订单就是前者（它高权限，但退的款是零）。"金额未知要按最坏情况办"那条
    规则住在理解层与工具层（``assess_risk`` 见到空金额判高风险、``create_refund``
    解析不出数字直接拒绝），放在这里会把取消订单一律拦成"额度不足"，
    而拦错的地方比放错的地方更难查。
    """
    if permission == "read":
        return ALLOWED

    limits = effective_limits(db, workspace_id)
    if limits["paused"]:
        return Verdict(
            allowed=False,
            reason=f"全局暂停中（{limits['pause_reason'] or '未注明原因'}），写操作一律不执行，转人工",
            triggers=("paused",),
        )

    if permission == "fund" and amount is not None:
        used = refund_used_today(db, workspace_id)
        remaining = limits["daily_refund_limit"] - used
        if amount > remaining:
            return Verdict(
                allowed=False,
                reason=(
                    f"当日退款额度不足：上限 {limits['daily_refund_limit']}，"
                    f"已用 {used}，本笔需要 {amount}"
                ),
                triggers=("daily_refund_limit",),
            )
    return ALLOWED


def pause(db: Session, workspace_id: str, *, actor_id: str, reason: str) -> CsGovernor:
    """一键全局暂停。留 actor 与 reason：这条是要被审计的东西，
    "谁在什么时候把写操作关掉了"出事故时必须能问出来。"""
    row = get_or_create(db, workspace_id)
    row.paused = True
    row.pause_reason = (reason or "")[:200] or None
    row.pause_updated_by = actor_id
    row.updated_at = naive_now()
    return row


def resume(db: Session, workspace_id: str, *, actor_id: str, reason: str = "") -> CsGovernor:
    row = get_or_create(db, workspace_id)
    row.paused = False
    row.pause_reason = None
    row.pause_updated_by = actor_id
    row.updated_at = naive_now()
    return row
