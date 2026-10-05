"""知识层的第二条通道：历史相似工单，以及跨工单的客户画像。

文档§2 把知识写成"政策 + 历史工单 RAG"，把记忆写成"跨工单记住客户偏好与历史
问题"。政策那半走仓库已有的混合检索（``KnowledgeService``），这一层补的是另外半：

**历史工单不进知识库。** 一张已办结的工单确实是组织知识，但它同时也是带有
归属的处置记录：谁客户、哪些订单、结论是否被人复核过。把它写成一篇知识库文档
会带来三个都不想要的后果——它进了所有人的检索作用区（私有工单尤其不该被同事
搜到）、它跟着文档生命周期走（删了就没了），而工单处置记录必须比文档长命。
所以这里直接从 ``tickets`` 查，用**能被追责的判据**而不是向量相似度。

为什么是判据而不是向量：相似度检索在这里给不出"像不像"的可解释答案。坐席要问
的是"这个客户上个月退过款吗""这单订单之前有没有人来过"，那些是精确条件查询，
能一次命中就一次命中，而向量相似度的排序会随嵌入模型换版而变——指标跟着抖，
却说不清为什么这两张单算相关。

画像（``CsCustomer.profile``）累计的是**已经发生过的处置**，不是模型的印象：
每条都带着出自哪张工单，版本自增，于是"当时看到的画像是第几版"能被轨迹记下来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from models import CsCustomer, Ticket
from services.clock import naive_now

# 画像里最多留几条历史处置。给的是"一屏能读完"的量：这个字段会被拼进模型上下文，
# 长了以后每轮都在付费，而第 20 条以前的处置对当前这张工单几乎不再有影响。
_PROFILE_HISTORY_LIMIT = 8

_TERMINAL = ("resolved", "closed")


@dataclass
class PriorTicket:
    """一条"以前发生过的相关工单"。带 ticket_id 是为了让坐席点回去看全轨迹。"""

    ticket_id: str
    channel: str
    intent: str | None
    status: str
    resolution: str | None
    summary: str | None
    reason: str
    created_at: Any

    def as_line(self) -> str:
        outcome = self.resolution or self.status
        return (
            f"- [{self.reason}] {self.created_at:%Y-%m-%d} {self.channel} "
            f"{self.intent or '未知意图'} → {outcome}："
            f"{(self.summary or '')[:120]}（工单 {self.ticket_id}）"
        )


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _order_numbers(ticket: Ticket | None) -> list[str]:
    entities = _loads(ticket.entities) if ticket is not None else {}
    numbers = entities.get("order_nos") or []
    return [str(number) for number in numbers if number]


def find_related(
    db: Session,
    workspace_id: str,
    *,
    customer_id: str | None = None,
    exclude_ticket_id: str | None = None,
    intent: str | None = None,
    order_numbers: list[str] | None = None,
    limit: int = 12,
) -> list[PriorTicket]:
    """同客户 / 同订单 / 同意图的既往工单。三路各取若干，去重后按时间倒序。

    三路分开取而不是"OR 一把捞"：一把捞的时候最近的那张单会挤掉所有名额，
    而"同一张订单上周来过一次"这种线索往往比"上周别的客户也投诉过"重要得多。
    """
    seen: dict[str, PriorTicket] = {}

    def collect(label: str, rows: list[Ticket]) -> None:
        for row in rows:
            if row.id in seen:
                # 已经由更强的那一路收进来了。保留先到的标签：
                # "同一张订单"比"同意图"更有解释力，所以按强到弱的顺序收集
                continue
            seen[row.id] = PriorTicket(
                ticket_id=row.id,
                channel=row.channel,
                intent=row.intent,
                status=row.status,
                resolution=row.resolution,
                summary=row.summary,
                reason=label,
                created_at=row.created_at,
            )

    def by_column(column: str, value: Any, cap: int) -> list[Ticket]:
        if not value:
            return []
        return (
            db.query(Ticket)
            .filter(
                Ticket.workspace_id == workspace_id,
                Ticket.id != (exclude_ticket_id or ""),
                getattr(Ticket, column) == value,
            )
            .order_by(Ticket.created_at.desc())
            .limit(cap)
            .all()
        )

    collect("同一客户", by_column("customer_id", customer_id, limit))

    for number in (order_numbers or [])[:3]:
        # 订单号在 entities 这个 JSON 文本列里，只能按子串匹配。这里不假装它是
        # 精确字段查询：订单号本身是定长带前缀的串，误匹配的概率极低，而为了
        # 这点严谨去建一张倒排表（ticket_orders）换来的可解释性并不值那一次写入。
        rows = (
            db.query(Ticket)
            .filter(
                Ticket.workspace_id == workspace_id,
                Ticket.id != (exclude_ticket_id or ""),
                Ticket.entities.like(f"%{number}%"),
            )
            .order_by(Ticket.created_at.desc())
            .limit(5)
            .all()
        )
        collect(f"同一订单 {number}", rows)

    if intent and intent != "other":
        rows = (
            db.query(Ticket)
            .filter(
                Ticket.workspace_id == workspace_id,
                Ticket.id != (exclude_ticket_id or ""),
                Ticket.intent == intent,
                Ticket.status.in_(_TERMINAL),
            )
            .order_by(Ticket.created_at.desc())
            .limit(5)
            .all()
        )
        collect(f"同类诉求（{intent}）", rows)

    ordered = sorted(seen.values(), key=lambda item: item.created_at, reverse=True)
    return ordered[:limit]


def render_history(lines: list[PriorTicket]) -> str:
    if not lines:
        return ""
    return "\n".join(line.as_line() for line in lines)


# ---------------------------------------------------------------------------
# 长期画像
# ---------------------------------------------------------------------------


def read_profile(customer: CsCustomer | None) -> dict[str, Any]:
    return _loads(customer.profile) if customer is not None else {}


def profile_brief(customer: CsCustomer | None) -> str:
    """给客户画像拼一段能进上下文的话。

    只说库里有的事实。写成一段而不是一个 JSON 块：模型读散文比读嵌套字典更稳，
    而这段每次都要进上下文，短一点是省每一轮的钱。
    """
    if customer is None:
        return ""
    profile = read_profile(customer)
    parts: list[str] = []
    if profile.get("preferences"):
        parts.append("偏好：" + "、".join(str(item) for item in profile["preferences"]))
    if profile.get("notes"):
        parts.append("注意点：" + "；".join(str(item) for item in profile["notes"][-2:]))
    history = profile.get("history") or []
    if history:
        recent = "、".join(
            f"{item.get('date', '')} {item.get('intent', '')}→{item.get('resolution', '')}"
            for item in history[-3:]
        )
        parts.append(f"近期处置：{recent}")
    return "客户画像（第 %s 版）：%s" % (
        customer.profile_version,
        "；".join(parts) if parts else "暂无积累",
    )


def record_outcome(
    db: Session,
    customer: CsCustomer | None,
    *,
    ticket: Ticket,
    resolution: str | None,
    note: str | None = None,
) -> CsCustomer | None:
    """把这张工单的处置结果累计进客户画像。

    **没有客户就不造一个**：工单可以匿名（只有订单号），这时候画像该保持空缺，
    而不是按邮箱的一半、电话的后四位去"认领"一个人。把处置写错客户身上比不写
    更难纠正——它会顺着画像影响之后每一张工单的判断。
    """
    if customer is None:
        return None
    profile = read_profile(customer)
    entry = {
        "date": ticket.created_at.strftime("%Y-%m-%d") if ticket.created_at else "",
        "ticket_id": ticket.id,
        "intent": ticket.intent,
        "resolution": resolution or ticket.status,
        "risk_level": ticket.risk_level,
    }
    if note:
        entry["note"] = note[:200]
    history = list(profile.get("history") or [])
    history = [item for item in history if item.get("ticket_id") != ticket.id]
    history.append(entry)
    profile["history"] = history[-_PROFILE_HISTORY_LIMIT:]
    if note:
        notes = list(profile.get("notes") or [])
        trimmed = note.strip()[:200]
        if trimmed and trimmed not in notes:
            notes.append(trimmed)
        profile["notes"] = notes[-_PROFILE_HISTORY_LIMIT:]
    customer.profile = json.dumps(profile, ensure_ascii=False)
    customer.profile_version = int(customer.profile_version or 0) + 1
    customer.updated_at = naive_now()
    return customer


def append_preference(
    db: Session, customer: CsCustomer | None, preference: str
) -> CsCustomer | None:
    """记住一条客户偏好（"不要打电话，发邮件"这类）。"""
    if customer is None or not preference.strip():
        return customer
    profile = read_profile(customer)
    preferences = list(profile.get("preferences") or [])
    value = preference.strip()[:120]
    if value not in preferences:
        preferences.append(value)
    profile["preferences"] = preferences[-_PROFILE_HISTORY_LIMIT:]
    customer.profile = json.dumps(profile, ensure_ascii=False)
    customer.profile_version = int(customer.profile_version or 0) + 1
    customer.updated_at = naive_now()
    return customer


def add_risk_flag(db: Session, customer: CsCustomer | None, flag: str) -> CsCustomer | None:
    """风险标记单独一列而不是塞进画像：风险分级要读它，而画像那段是给模型看的散文。"""
    if customer is None or not flag.strip():
        return customer
    try:
        flags = json.loads(customer.risk_flags) if customer.risk_flags else []
    except (TypeError, ValueError):
        flags = []
    if not isinstance(flags, list):
        flags = []
    value = flag.strip()[:60]
    if value not in flags:
        flags.append(value)
        customer.risk_flags = json.dumps(flags, ensure_ascii=False)
        customer.updated_at = naive_now()
    return customer


def load_customer(db: Session, workspace_id: str, ticket: Ticket) -> CsCustomer | None:
    if not ticket.customer_id:
        return None
    return (
        db.query(CsCustomer)
        .filter(
            CsCustomer.workspace_id == workspace_id, CsCustomer.id == ticket.customer_id
        )
        .first()
    )
