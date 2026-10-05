"""接入层：把各渠道送来的原始输入归一化成一张工单。

对应文档工作流第 1 步"接收工单 → 标准化输入（渠道、内容、用户ID、附件）"。

**这一层只归一化渠道已经结构化交给我们的东西**（发件人地址、来电号码、APP 的
userId、转发的邮件 ID）。从正文里把订单号、金额、诉求类型、情绪抠出来是
``services/ticket/understand.py`` 的事——把两者混在一层，就没法在 LLM 不可用时
单独保住"至少知道这是谁的工单"这条底线，而那是转人工能正确路由的前提。

三件事在这一层承担，每件都有对应的失败模式：

1. **身份归一**。同一个人可以从网页聊天（userId）、邮件（地址）、企微（external
   userid）三路进来。邮箱小写、电话削掉 ``+86``/``0086``/空格横线，否则他会因为
   写了国际前缀而被登记成两个人——两份画像、两个额度、两张工单历史，跨工单记忆
   这条能力当场失效，而且失效得毫无症状。
2. **重投递去重**。邮件会被转发、IM 会重连、 webhook 会重试。带了 ``external_ref``
   的同一封邮件重复投递只应该建出一张工单，否则同一条投诉被处理三遍——其中退款
   那类操作重复三遍是事故而不是瑕疵。
3. **正文清洗**。转发邮件里九成是被引述的历史正文。不去掉它，检索会拿旧邮件的
   内容去回答新问题，而抽取到的"订单号"可能是三个月前那单的。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from models import CsCustomer, Ticket
from services.clock import naive_now, to_naive
from services.ticket.trace import append_event

# intake 愿意接受的渠道标签。它是**标签**而不是适配器：真正的入口在 router 里，
# 没接通的渠道不会自己产生流量，所以这里不需要再维护一份"已实现"清单。
CHANNELS = frozenset({"web_chat", "email", "app", "wecom", "phone", "api"})


class TicketIntakeError(ValueError):
    """输入本身不成立。区别于工单后续处理失败——那属于业务，不是接入。"""


@dataclass(frozen=True)
class TicketIntake:
    """一个渠道送来的原始输入。渠道适配器负责填它，填不出来就该报错而不是猜。"""

    channel: str
    content: str
    subject: str | None = None
    # 渠道侧的消息/邮件标识。有它就够去重，没有就不去重（宁可重复也别漏）
    external_ref: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    # APP / 企微给的自家用户标识
    customer_ref: str | None = None
    attachments: tuple[str, ...] = ()
    # 渠道侧的收件时刻。缺省用应用时钟
    received_at: datetime | None = None
    # 适配器自带的原始元数据，落进 entities 里作为抽取的起点（不是垃圾字段袋：
    # 它只放渠道能直接说明的事实，比如邮件的 message-id、电话的转写时长）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntakeResult:
    ticket: Ticket
    # False = 命中了已有工单（重投递），没有新建任何东西
    created: bool
    customer_id: str | None
    # 命中去重时指向的那张工单
    deduped_against: str | None = None


def enabled_channels() -> frozenset[str]:
    """配置白名单与 ``CHANNELS`` 的交集。

    交集而不是直接信任配置：``TICKET_CHANNELS=emial`` 这么一个手抄错的值，
    如果直接采信，工单会以一个不存在的渠道名建进库里，之后所有按渠道筛选的
    指标都看不见它。宁可让这条渠道静默关闭，并在启动自检里看得见。
    """
    listed = {part.strip() for part in settings.TICKET_CHANNELS.split(",") if part.strip()}
    return frozenset(listed & CHANNELS)


def normalize_email(value: str | None) -> str | None:
    """邮箱归一为小写。

    严格说 local-part 大小写敏感（RFC 5321），实践上没有任何主流服务商区分它，
    而区分大小的后果是同一个客户裂成两行——这里要的是身份匹配，不是投递正确性。
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned or "@" not in cleaned:
        return None
    # "Name <a@b.c>" 这种带显示名的形式在邮件头里最常见
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", cleaned)
    return match.group(0) if match else None


_CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")


def normalize_phone(value: str | None) -> str | None:
    """电话归一：只留数字，去掉中国国家的码前缀。

    削法是中国移动号码专属的：去分隔符、去 ``+86`` / ``0086`` / 前导 ``0``，
    剩下 11 位且形如 ``1[3-9]xxxxxxxx`` 才认。其它国家的号码**原样保留数字串**
    而不是套一个猜的国家码——猜错的号码会让两个不同的人匹配成同一个客户，
    那比两个不同的人没匹配上严重得多。
    """
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    for prefix in ("0086", "86"):
        if digits.startswith(prefix) and len(digits) > 11:
            digits = digits[len(prefix) :]
            break
    if _CN_MOBILE.match(digits):
        return digits
    return digits


def _strip_quoted_history(content: str) -> str:
    """去掉转发/回复邮件里被引述的旧正文。

    两条判据，都偏保守：整行以 ``>`` 开头的删掉；撞上"分隔线 + 原始邮件/On ...
    wrote"这一类标记行的，从该行起整个截断。宁可留一段引述（检索会多几条不相干的
    命中，症状是可查的），也不腰斩客户的新增诉求（症状是不可见的）。
    """
    markers = re.compile(
        r"^\s*(-{3,}|_{3,}|={3,})\s*$|^\s*(-{3,}|_{3,}|={3,})\s*"
        r"(原始邮件|Original Message|Forwarded message|回复的邮件)"
        r"|^\s*(在|On)\s+\S.*\s(写道|wrote)\s*[::]\s*$",
        re.I | re.M,
    )
    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        if line.lstrip().startswith(">"):
            continue
        if markers.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def normalize_content(content: str, *, channel: str) -> str:
    """清洗正文：统一换行、去控制字符、坍缩空行、邮件去引述。"""
    if content is None:
        raise TicketIntakeError("工单正文为空")
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # 只留 \t 与 \n：\x00 之类的控制字符进了 Text 列，前端渲染与 BM25 分词都会
    # 在看不见的地方出错
    text = "".join(ch for ch in text if ch == "\t" or ch == "\n" or ord(ch) >= 32)
    if channel == "email":
        text = _strip_quoted_history(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise TicketIntakeError("工单正文清洗后为空（只有引述或空白）")
    limit = settings.TICKET_INTAKE_MAX_CHARS
    if len(text) > limit:
        # 明确报错而不是截断：被切掉的尾部往往正是订单号与诉求，而一张缺订单号
        # 的工单会走到"查不到订单"再转人工——看起来安全，实际是我们扔了信息
        raise TicketIntakeError(
            f"工单正文 {len(text)} 字符，超过上限 {limit}。"
            "请只提交本次诉求，不要把整条邮件历史转发进来。"
        )
    return text


def normalize_attachments(names: Any) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str):
        names = [part.strip() for part in names.split(",") if part.strip()]
    cleaned = tuple(str(name).strip() for name in names if str(name).strip())
    limit = settings.TICKET_INTAKE_MAX_ATTACHMENTS
    if len(cleaned) > limit:
        raise TicketIntakeError(
            f"附件 {len(cleaned)} 个，超过上限 {limit}。"
            "多半是把整封邮件线程连着附件一起转发了。"
        )
    return cleaned


def _identity_keys(intake: TicketIntake) -> dict[str, str]:
    """按优先级给出可用的身份键。优先级即证据强度：渠道自家 ID > 邮箱 > 电话。"""
    keys: dict[str, str] = {}
    external = (intake.customer_ref or "").strip()
    if external:
        keys["external_ref"] = external
    email = normalize_email(intake.customer_email)
    if email:
        keys["email"] = email
    phone = normalize_phone(intake.customer_phone)
    if phone:
        keys["phone"] = phone
    return keys


def _find_customer(db: Session, workspace_id: str, keys: dict[str, str]) -> tuple[CsCustomer | None, bool]:
    """按最强的一个键找人。

    返回 ``(客户或 None, 是否有歧义)``。**既不自动合并、也不再造新的一行**：
    同一个人被登记过两次时，挑一行写画像是替客户决定"哪一行是真的"，而再建第三行
    会把这个问题加重成三个。有歧义就不挂客户，并让调用方在轨迹里记下这一件——
    它是"同一个客户两张历史工单"这类唯一可见入口，等着人来并。
    """
    column_by_key = {
        "external_ref": CsCustomer.external_ref,
        "email": CsCustomer.email,
        "phone": CsCustomer.phone,
    }
    for name in ("external_ref", "email", "phone"):
        value = keys.get(name)
        if not value:
            continue
        column = column_by_key[name]
        rows = (
            db.query(CsCustomer)
            .filter(CsCustomer.workspace_id == workspace_id, column == value)
            .all()
        )
        if len(rows) == 1:
            return rows[0], False
        if len(rows) > 1:
            return None, True
    return None, False


def _create_customer(db: Session, workspace_id: str, keys: dict[str, str]) -> CsCustomer | None:
    if not keys:
        return None
    customer = CsCustomer(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        external_ref=keys.get("external_ref"),
        email=keys.get("email"),
        phone=keys.get("phone"),
        display_name=keys.get("email") or keys.get("phone") or keys.get("external_ref"),
        created_at=naive_now(),
        updated_at=naive_now(),
    )
    db.add(customer)
    return customer


def _derive_subject(intake: TicketIntake, content: str) -> str | None:
    if intake.subject and intake.subject.strip():
        return intake.subject.strip()[:500]
    # 网页聊天与 API 通常没有主题，用第一行顶上。它只服务于队列里的一瞥，
    # 不参与任何判定，所以不值得为它做摘要模型
    first_line = next((line.strip() for line in content.split("\n") if line.strip()), "")
    return first_line[:200] if first_line else None


def submit_ticket(
    db: Session, intake: TicketIntake, *, workspace_id: str, assignee_id: str | None = None
) -> IntakeResult:
    """受理一张工单：归一化 → 去重 → 认人 → 建档 → 记下第一步轨迹。

    工单、客户、轨迹三条写在**同一个事务**里提交。分次提交会得到"有工单但回放
    没有起点"或者"客户建了工单没建"的中间态，而这两种都只在下一次看指标时才发现。
    """
    if not workspace_id:
        raise TicketIntakeError("缺少 workspace_id：工单是组织资产，没有归属就是无主流浪")
    if intake.channel not in CHANNELS:
        raise TicketIntakeError(
            f"未知渠道 {intake.channel!r}，可选：{sorted(CHANNELS)}"
        )
    allowed = enabled_channels()
    if intake.channel not in allowed:
        raise TicketIntakeError(
            f"渠道 {intake.channel!r} 未启用（TICKET_CHANNELS={settings.TICKET_CHANNELS}）"
        )

    content = normalize_content(intake.content, channel=intake.channel)
    attachments = normalize_attachments(intake.attachments)
    keys = _identity_keys(intake)

    external_ref = (intake.external_ref or "").strip() or None
    if external_ref:
        existing = (
            db.query(Ticket)
            .filter(
                Ticket.workspace_id == workspace_id,
                Ticket.channel == intake.channel,
                Ticket.external_ref == external_ref,
            )
            .first()
        )
        if existing is not None:
            return IntakeResult(
                ticket=existing, created=False, customer_id=existing.customer_id,
                deduped_against=existing.id,
            )

    customer, ambiguous = _find_customer(db, workspace_id, keys)
    if customer is not None:
        resolved = customer
    elif ambiguous:
        # 有歧义时不新建：那时缺的不是"一行客户记录"，是"哪几行其实是同一个人"这个答案
        resolved = None
    else:
        resolved = _create_customer(db, workspace_id, keys)

    now = to_naive(intake.received_at) if intake.received_at else naive_now()
    sla_hours = settings.TICKET_SLA_HOURS
    ticket = Ticket(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        customer_id=resolved.id if resolved else None,
        assignee_id=assignee_id,
        channel=intake.channel,
        external_ref=external_ref,
        status="new",
        # risk_level 留空：NULL 说的是"还没评估过"，与"评为低风险"是两回事
        request_text=content,
        subject=_derive_subject(intake, content),
        attachments=json.dumps(list(attachments), ensure_ascii=False) if attachments else None,
        # 身份键与渠道元数据先占上 entities，抽取层之后往同一个对象里补订单号等。
        # 起步就写而不是等抽取：抽取失败时，"这是谁"仍然查得到
        entities=json.dumps(
            {"identity": keys, "channel_metadata": intake.metadata}, ensure_ascii=False
        ),
        tool_rounds=0,
        created_at=now,
        updated_at=now,
        sla_due_at=now + timedelta(hours=sla_hours) if sla_hours > 0 else None,
    )
    db.add(ticket)
    db.flush()

    note = f"从 {intake.channel} 收到工单，正文 {len(content)} 字符"
    if attachments:
        note += f"，附件 {len(attachments)} 个"
    if ambiguous:
        note += "；客户身份有多个候选，未挂画像等待人工确认"
    append_event(
        db,
        ticket,
        node="intake",
        kind="decision",
        status="ok",
        message=note,
    )
    db.commit()
    return IntakeResult(
        ticket=ticket, created=True, customer_id=ticket.customer_id,
        deduped_against=None,
    )
