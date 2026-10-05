"""理解层：从工单正文里抽出事实，并据此定风险等级。

对应文档工作流的第 2、3 步。两条通道，职责不同：

- **规则层**（``extract_by_rules``）：订单号、金额、意图、关键词命中。它不要模型、
  不要网络、不要钱，因此也就是 LLM 全线不可用时**仍然能把工单分对人**的那条底线。
- **模型层**（``_llm_understand``）：补规则补不到的东西——商品名、情绪、把口语诉求
  归到意图清单上。它是增强不是依赖，失败就退回规则层并留下 warning。

## 风险判定为什么完全不交给模型

``assess_risk`` 是阈值与关键词的确定性函数。让模型参与"该不该人审"，等于把
文档里那个**错误操作率**指标交给它当天对措辞的同情心——"客户看起来很着急，
这笔 8000 块的退款应该没问题吧"这类判断既不能 review 也不能追责。
所以提示词里刻意不问模型"要不要转人工"（见 prompts/ticket_understand/v1.md）。

## 金额未知时按高风险处理

"我要退款"这句话里没有任何数字。判成低风险、让模型自己决定退多少，是错误操作率
最省事的来源。这里的方向和 ``REVIEW_EVIDENCE_MAX_CHARS`` 那类取舍一致：
**宁可多转人工，也不在证据不足时自动放行**。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, field_validator

from config import settings
from models import Ticket
from services import prompt_library, structured
from services.ticket.trace import append_event

logger = logging.getLogger("ticket.understand")

INTENTS = (
    "query_order",
    "query_logistics",
    "change_address",
    "invoice",
    "refund",
    "cancel_order",
    "complaint",
    "other",
)
SENTIMENTS = ("calm", "unhappy", "hostile")
RISK_LEVELS = ("low", "mid", "high")

_RISK_ORDER = {"low": 0, "mid": 1, "high": 2}

# 意图 → 基线风险。分类依据是文档§6.1 的权限三档：查询低、修改中、资金高。
# complaint 归中：它没有写操作，但"纯投诉"是最容易在下一句变成曝光的东西。
_INTENT_RISK = {
    "query_order": "low",
    "query_logistics": "low",
    "change_address": "mid",
    "invoice": "mid",
    "complaint": "mid",
    "refund": "high",
    "cancel_order": "high",
    "other": "mid",
}

# 需要金额门槛才能自动放行的意图。只有退款：取消订单的代价是营收而不是现金，
# 它一律走人审（文档把它和退款并列在高风险里）。
_AMOUNT_GATED = frozenset({"refund"})

# 规则层的意图关键词。**按顺序**匹配，先具体后泛化：一段话里同时出现"退款"和
# "发票"时，要的是退款——那是代价更高的那件，判高了最多多一次人工。
_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("refund", ("退款", "退钱", "退全款", "给我退", "帮我退", "我要退", "把款退", "退了")),
    ("cancel_order", ("取消订单", "不要了", "撤单", "关掉订单")),
    ("change_address", ("改地址", "换地址", "地址写错", "地址错了", "改电话", "换个地址", "收货信息")),
    ("invoice", ("发票", "开票", "抬头", "税号")),
    ("query_logistics", ("物流", "快递", "到哪", "发货", "运单", "签收")),
    ("query_order", ("订单状态", "查订单", "订单号", "我的订单")),
    ("complaint", ("投诉", "举报", "态度", "客服态度")),
)

# 订单号：带字母前缀的编号，或者一段 10~20 位的纯数字。
_CODED_ORDER = re.compile(r"\b[A-Z]{2,6}[-_]?\d{5,}\b", re.I)
_BARE_DIGITS = re.compile(r"(?<![\dA-Za-z.])\d{10,20}(?![\dA-Za-z.])")
# 纯数字那一路必须排掉手机号：11 位、1 开头、第二位 3-9。不排掉的话，客户留的
# 联系电话会被当成订单号去查订单，然后"查无此单"会把一张改地址的工单带偏。
_CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")

# 金额：¥300 / 300元 / 300块 / 人民币300元。刻意**不**处理"三百"这种中文数字——
# 猜出来的金额进风险判定比没有金额更糟，因为它是"看起来有依据"的错误数字。
_AMOUNT = re.compile(
    r"(?:[¥￥]\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:元|块|块钱|RMB|rmb|人民币))"
)


class TicketUnderstanding(BaseModel):
    """模型层的输出契约。字段全是"原文里看到的事实"，不含任何决定。"""

    intent: str = "other"
    product: str = ""
    sentiment: str = "calm"
    order_nos: list[str] = Field(default_factory=list)
    amount: str = ""
    summary: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _known_intent(cls, value: Any) -> str:
        # 编造的意图不报错而是归到 other：other 的风险是 mid，会走人审。
        # 让整个理解层因为一个字段值不合格而失败退回，等于把能用的事实也扔了。
        text = str(value or "").strip()
        return text if text in INTENTS else "other"

    @field_validator("sentiment", mode="before")
    @classmethod
    def _known_sentiment(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if text in SENTIMENTS else "calm"


@dataclass
class Extracted:
    """规则层或合并后的事实集合。"""

    intent: str = "other"
    product: str = ""
    sentiment: str = "calm"
    order_nos: list[str] = field(default_factory=list)
    amount: Decimal | None = None
    summary: str = ""
    legal_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    # 事实来自哪里。风险面板上要能说"这个金额是原文里读到的"还是"模型说的"
    source: str = "rules"

    def to_entities(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "product": self.product,
            "sentiment": self.sentiment,
            "order_nos": list(self.order_nos),
            "amount": str(self.amount) if self.amount is not None else None,
            "summary": self.summary,
            "legal_hits": list(self.legal_hits),
            "negative_hits": list(self.negative_hits),
            "source": self.source,
        }


@dataclass
class RiskAssessment:
    level: str
    requires_human: bool
    triggers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "requires_human": self.requires_human,
            "triggers": list(self.triggers),
        }


def _keyword_list(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def extract_order_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _CODED_ORDER.finditer(text):
        token = match.group(0)
        if token not in found:
            found.append(token)
    for match in _BARE_DIGITS.finditer(text):
        token = match.group(0)
        if _CN_MOBILE.match(token):
            continue
        if token not in found:
            found.append(token)
    return found


def parse_amount(value: Any) -> Decimal | None:
    """把 "1,200元" / "¥300" / 300 / Decimal("300") 削成一个数。失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value >= 0 else None
    digits = re.sub(r"[^\d.]", "", str(value))
    if not digits or digits.count(".") > 1:
        return None
    try:
        parsed = Decimal(digits)
    except InvalidOperation:
        return None
    return parsed if parsed >= 0 else None


def extract_amount(text: str) -> Decimal | None:
    """正文里出现的所有金额。多个时取**最大**的那个作为风险依据。

    就高不就低：客户写"上次退了 50，这次这单 800 要全退"时，按 800 判。
    按第一个匹配判，等于让句子里出现的顺序决定要不要人审。
    """
    amounts = [
        parse_amount(match.group(1) or match.group(2))
        for match in _AMOUNT.finditer(text)
    ]
    usable = [amount for amount in amounts if amount is not None]
    return max(usable) if usable else None


def match_intent(text: str) -> str:
    for intent, keywords in _INTENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return intent
    return "other"


def _hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def extract_by_rules(text: str) -> Extracted:
    """纯确定性抽取。不需要模型、不需要网络，是 LLM 不可用时的底线。"""
    body = text or ""
    sentiment = "calm"
    negative_hits = _hits(body, _keyword_list(settings.TICKET_NEGATIVE_KEYWORDS))
    if negative_hits:
        # 规则层最高只到 unhappy：hostile 是"要不要因为情绪转人工"的触发词，
        # 让关键词表决定这件事会让一个写了"太差"的客户被误判成暴怒。
        # 这个降级面是明确的——情绪判定只在模型那条通道上有。
        sentiment = "unhappy"
    return Extracted(
        intent=match_intent(body),
        order_nos=extract_order_numbers(body),
        amount=extract_amount(body),
        sentiment=sentiment,
        legal_hits=_hits(body, _keyword_list(settings.TICKET_LEGAL_KEYWORDS)),
        negative_hits=negative_hits,
        source="rules",
    )


def _appears_in_text(value: str, text: str) -> bool:
    return value.casefold() in text.casefold()


def _digits_visible(value: str, text: str) -> bool:
    """数字在原文里对得上吗——忽略千分位分隔符与空白。

    ``1,200`` 和 ``1200`` 是同一个数的两种排版，把它们算对不上会让规则已经抽到的
    金额被自己的校验扔掉。除此之外不做任何模糊匹配：这条检查挡的是编造，
    而编造出来的订单号会被拿去查别人的单，下一步就是退错人的钱。
    """
    haystack = re.sub(r"[,，\s]", "", text)
    return re.sub(r"[,，\s]", "", value) in haystack


def merge_with_llm(rules: Extracted, llm: TicketUnderstanding | None, text: str) -> Extracted:
    """把模型说的和原文里真有的对一遍再合并。

    **模型给出的订单号和金额必须在原文里逐字找到，否则丢掉。** 这一步不是防模型
    手滑，是防一个具体的事故：编出来的订单号会被拿去查单、查到的如果是别人的单，
    下一步退款就是退别人的钱。意图和情绪不校验——它们是归类判断不是事实陈述。
    """
    if llm is None:
        return rules
    verified_orders = [
        order for order in llm.order_nos if order and _appears_in_text(order, text)
    ]
    hallucinated = [order for order in llm.order_nos if order not in verified_orders]
    if hallucinated:
        logger.warning(
            "dropped %d order number(s) not present in the ticket text: %s",
            len(hallucinated),
            hallucinated[:3],
        )

    amount = rules.amount
    if amount is None:
        llm_amount = parse_amount(llm.amount)
        if llm_amount is not None and _digits_visible(
            re.sub(r"\.0+$", "", str(llm_amount)), text
        ):
            amount = llm_amount
        elif llm_amount is not None:
            logger.warning("dropped amount %s not found verbatim in ticket text", llm_amount)

    order_nos = list(rules.order_nos)
    for order in verified_orders:
        if order not in order_nos:
            order_nos.append(order)

    return Extracted(
        # other 不覆盖已经识别出的意图：模型给不出更好的分类时，规则那个更具体的
        # 命中更有用（"other" 和 "refund" 谁的风险高是反的，靠覆盖会抖）
        intent=llm.intent if llm.intent != "other" else rules.intent,
        product=(llm.product or "").strip() or rules.product,
        sentiment=llm.sentiment if llm.sentiment != "calm" else rules.sentiment,
        order_nos=order_nos,
        amount=amount,
        summary=(llm.summary or "").strip() or rules.summary,
        legal_hits=rules.legal_hits,
        negative_hits=rules.negative_hits,
        source="rules+llm",
    )


def assess_risk(
    facts: Extracted,
    *,
    consecutive_tool_failures: int = 0,
) -> RiskAssessment:
    """定风险等级。确定性函数：同样的输入永远是同样的输出。

    触发条件按文档§6.2 的四条来，其中"工具连续失败 2 次"需要运行时信息，
    所以它是一个入参而不是这里自己数——数它的是编排层。
    """
    triggers: list[str] = []
    level = _INTENT_RISK.get(facts.intent, "mid")

    if facts.intent in _AMOUNT_GATED:
        if facts.amount is None:
            # 金额未知就按高风险。"我要退款"里没有数字，判成低风险让模型自己
            # 决定退多少，是错误操作率最省事的来源
            level = "high"
            triggers.append("amount_unknown")
        elif facts.amount >= Decimal(str(settings.TICKET_REFUND_REVIEW_THRESHOLD)):
            level = "high"
            triggers.append("amount_over_threshold")

    if facts.legal_hits:
        level = "high"
        triggers.append("legal_keywords")

    if facts.sentiment == "hostile":
        level = "high"
        triggers.append("hostile_sentiment")

    if consecutive_tool_failures >= 2:
        # 不升级 level：这一条要的是"停手交人"，而不是"这件事本来就很贵"。
        # 把两者混进 level 会让下一次重算风险时被上一次的工具失败污染。
        triggers.append("tool_failures")

    return RiskAssessment(
        level=level,
        requires_human=level == "high" or bool(triggers),
        triggers=triggers,
    )


async def _llm_understand(adapter: Any, *, text: str, model: str) -> TicketUnderstanding | None:
    """问模型一次。失败退回 None，让规则层的结果照常可用，但一定留日志。

    "增强的静默降级"是这仓库里最难查的一类故障（规划、改写、记忆抽取都栽过），
    所以这里连 warning 都带上 attempts——只说"降级了"分辨不出是模型没答上还是
    压根没答全。
    """
    prompt = prompt_library.render("ticket_understand", ticket_text=text[:4000])
    result, report = await structured.request_structured(
        adapter,
        schema=TicketUnderstanding,
        prompt=prompt,
        model=model,
        purpose="ticket_understand",
        array=False,
        temperature=0.0,
    )
    if result is None:
        logger.warning(
            "ticket understanding degraded to rules-only: attempts=%s failures=%s "
            "finish_reason=%s",
            report.attempts,
            report.failures,
            report.finish_reason,
        )
    return result


async def understand_ticket(
    db,
    ticket: Ticket,
    *,
    adapter: Any = None,
    model: str | None = None,
    consecutive_tool_failures: int = 0,
) -> tuple[Extracted, RiskAssessment]:
    """抽取 + 定级 + 落库 + 记两步轨迹。

    **不 commit**：这两个节点在状态机里是一个安全点，由编排层决定和谁一起提交。
    """
    facts = extract_by_rules(ticket.request_text)
    use_llm = adapter is not None and settings.TICKET_UNDERSTAND_LLM
    if use_llm:
        llm = await _llm_understand(
            adapter, text=ticket.request_text, model=model or settings.utility_model
        )
        facts = merge_with_llm(facts, llm, ticket.request_text)

    if not facts.summary:
        facts.summary = (ticket.subject or facts.intent)

    risk = assess_risk(facts, consecutive_tool_failures=consecutive_tool_failures)

    ticket.intent = facts.intent
    ticket.risk_level = risk.level
    ticket.summary = facts.summary[:1000]
    # entities 在接入层已经写了身份键与渠道元数据，这里是**补**而不是换：
    # 订单号查完了还要能回说是谁发的工单
    entities = _load_entities(ticket)
    entities.update(facts.to_entities())
    entities["risk"] = risk.to_dict()
    ticket.entities = json.dumps(entities, ensure_ascii=False)

    append_event(
        db,
        ticket,
        node="understand",
        kind="thinking",
        status="ok",
        message=(
            f"意图 {facts.intent}，订单号 {facts.order_nos or '无'}，"
            f"金额 {facts.amount if facts.amount is not None else '未给出'}，"
            f"情绪 {facts.sentiment}（来源 {facts.source}）"
        ),
    )
    append_event(
        db,
        ticket,
        node="risk",
        kind="decision",
        status="ok",
        message=f"风险 {risk.level}，{'需要人审' if risk.requires_human else '可自动执行'}"
        + (f"，触发：{','.join(risk.triggers)}" if risk.triggers else ""),
    )
    return facts, risk


def _load_entities(ticket: Ticket) -> dict[str, Any]:
    if not ticket.entities:
        return {}
    try:
        loaded = json.loads(ticket.entities)
    except (TypeError, ValueError):
        # 解析不动就当没有：抽取层接着往下写会比"整张工单因为一个坏 JSON
        # 而处理不了"更有用，而坏在哪一行有 ticket.id 可查
        logger.warning("ticket %s has unparseable entities; starting fresh", ticket.id)
        return {}
    return loaded if isinstance(loaded, dict) else {}
