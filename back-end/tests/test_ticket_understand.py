"""理解层：规则抽取、模型合并与防编造、风险分级。

分两组：纯函数那组不需要库（判定的正确性不该依赖数据库），
``understand_ticket`` 那组用 ``db_real``（要断言落库与轨迹）。
"""
from decimal import Decimal
import json
import logging

import pytest

from config import settings
from conftest import ScriptedAdapter, run
from models import Ticket, TicketEvent
from services.clock import naive_now
from services.ticket import understand
from services.ticket.understand import Extracted, TicketUnderstanding


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_REFUND_REVIEW_THRESHOLD", 200.0)
    monkeypatch.setattr(settings, "TICKET_UNDERSTAND_LLM", True)
    monkeypatch.setattr(settings, "TICKET_NEGATIVE_KEYWORDS", "垃圾,太差,骗子,投诉")
    monkeypatch.setattr(settings, "TICKET_LEGAL_KEYWORDS", "律师,起诉,315,曝光")
    # 重试关掉：ScriptedAdapter 的脚本用尽就 assert，留一次重试等于每条
    # "模型没答好"的用例都要多写一轮同样的坏输出
    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 0)


def _ticket(db, text: str) -> Ticket:
    now = naive_now()
    ticket = Ticket(
        id="t1",
        workspace_id="w1",
        channel="web_chat",
        status="new",
        request_text=text,
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_规则抽取订单号金额与意图():
    facts = understand.extract_by_rules(
        "订单 ORD20260115001 买了个耳机 350元，三天了还没发货，帮我查下物流"
    )
    assert facts.order_nos == ["ORD20260115001"]
    assert facts.amount == Decimal("350")
    assert facts.intent == "query_logistics"
    assert facts.sentiment == "calm"


def test_手机号不会被当成订单号():
    facts = understand.extract_by_rules("我电话 13800138000，订单 8123456789012 要改地址")
    assert facts.order_nos == ["8123456789012"]
    assert facts.intent == "change_address"


def test_多个金额取最大的那个作风险依据():
    facts = understand.extract_by_rules("上次退了 50 元，这次这单 800 元要全退")
    assert facts.amount == Decimal("800")


def test_中文数字金额不猜():
    assert understand.extract_by_rules("退款三百元").amount is None


def test_同时说要退款和要发票时按代价高的那个判():
    assert understand.extract_by_rules("发票先不急，把款退了").intent == "refund"


def test_关键词命中记的是命中项而不是只有真假():
    facts = understand.extract_by_rules("再不处理我就找律师，还要上 315")
    assert facts.legal_hits == ["律师", "315"]


def test_规则层情绪最高只到unhappy():
    facts = understand.extract_by_rules("你们这服务太差了，垃圾")
    assert facts.sentiment == "unhappy"
    assert set(facts.negative_hits) == {"太差", "垃圾"}


def test_意图清单外的值归到other而不是报错():
    assert TicketUnderstanding(intent="发火箭").intent == "other"
    assert TicketUnderstanding(sentiment="furious").sentiment == "calm"


def test_模型编出来的订单号被丢掉而原文里的留下():
    rules = understand.Extracted(order_nos=["ORD111"])
    llm = TicketUnderstanding(
        intent="refund", order_nos=["ORD111", "ORD9999999999"], summary="要退款"
    )
    merged = understand.merge_with_llm(rules, llm, "订单 ORD111 我要退款")
    assert merged.order_nos == ["ORD111"]
    assert merged.intent == "refund"


def test_模型补的金额也要逐字对得上():
    rules = Extracted(amount=None)
    llm = TicketUnderstanding(amount="1200")
    assert understand.merge_with_llm(rules, llm, "退款 1,200 元").amount == Decimal("1200")
    # 原文里根本没有 1200 这个数字
    assert understand.merge_with_llm(rules, llm, "退款 300 元").amount is None


def test_other不覆盖已经识别出的意图():
    rules = Extracted(intent="refund")
    merged = understand.merge_with_llm(rules, TicketUnderstanding(intent="other"), "我要退款")
    assert merged.intent == "refund"


def test_没有模型结果时规则结果原样返回():
    rules = Extracted(intent="invoice", legal_hits=["律师"])
    assert understand.merge_with_llm(rules, None, "随便") is rules


def _facts(**overrides) -> Extracted:
    base = {"intent": "other", "amount": None, "sentiment": "calm"}
    base.update(overrides)
    return Extracted(**base)


def test_查询类是低风险可自动执行():
    risk = understand.assess_risk(_facts(intent="query_logistics"))
    assert (risk.level, risk.requires_human, risk.triggers) == ("low", False, [])


def test_改地址是中风险():
    risk = understand.assess_risk(_facts(intent="change_address"))
    assert (risk.level, risk.requires_human) == ("mid", False)


def test_退款金额未知按高风险():
    risk = understand.assess_risk(_facts(intent="refund"))
    assert risk.level == "high"
    assert "amount_unknown" in risk.triggers


def test_退款超过阈值走人审而小额退款不必():
    over = understand.assess_risk(_facts(intent="refund", amount=Decimal("800")))
    assert ("amount_over_threshold" in over.triggers) and over.requires_human
    # 小额退款仍按意图基线走：refund 本身就是资金类，文档把它列在高风险里
    under = understand.assess_risk(_facts(intent="refund", amount=Decimal("50")))
    assert under.level == "high" and not under.triggers


def test_取消订单一律高风险():
    assert understand.assess_risk(_facts(intent="cancel_order")).level == "high"


def test_法律关键词把任何意图抬到高风险():
    risk = understand.assess_risk(_facts(intent="query_order", legal_hits=["律师"]))
    assert risk.level == "high"
    assert risk.triggers == ["legal_keywords"]


def test_暴怒客户转人工():
    risk = understand.assess_risk(_facts(intent="query_order", sentiment="hostile"))
    assert risk.requires_human and "hostile_sentiment" in risk.triggers


def test_工具连续失败两次只要求停手不抬高事件贵贱():
    risk = understand.assess_risk(_facts(intent="query_order"), consecutive_tool_failures=2)
    assert risk.level == "low"
    assert risk.requires_human and risk.triggers == ["tool_failures"]


def test_理解结果写回工单并留下两步轨迹(db_real):
    ticket = _ticket(db_real, "订单 ORD20260115001 的耳机 350 元，我要退款")
    facts, risk = run(understand.understand_ticket(db_real, ticket))
    db_real.commit()

    assert ticket.intent == "refund"
    assert ticket.risk_level == "high"
    entities = json.loads(ticket.entities)
    assert entities["order_nos"] == ["ORD20260115001"]
    assert entities["amount"] == "350"
    assert entities["risk"]["level"] == "high"

    events = (
        db_real.query(TicketEvent).filter_by(ticket_id="t1").order_by(TicketEvent.seq).all()
    )
    assert [event.node for event in events] == ["understand", "risk"]
    assert events[1].kind == "decision"


def test_模型通道补上商品名与情绪(db_real, monkeypatch):
    adapter = ScriptedAdapter(
        [
            {
                "text": (
                    '{"intent": "refund", "product": "蓝牙耳机", "sentiment": "hostile",'
                    ' "order_nos": ["ORD20260115001"], "amount": "",'
                    ' "summary": "客户要求退订单里的耳机款"}'
                )
            }
        ]
    )
    ticket = _ticket(db_real, "ORD20260115001 那单给我退款，你们就是骗子")
    facts, risk = run(understand.understand_ticket(db_real, ticket, adapter=adapter))
    assert facts.product == "蓝牙耳机"
    assert facts.source == "rules+llm"
    # hostile 只有模型那条通道能给出来，而它把风险抬到人审
    assert facts.sentiment == "hostile"
    assert risk.requires_human and "hostile_sentiment" in risk.triggers


def test_模型挂了退回规则结果并留下warning(db_real, caplog):
    adapter = ScriptedAdapter([{"text": "不是一段 JSON"}])
    ticket = _ticket(db_real, "ORD20260115555 要退款 900 元")
    with caplog.at_level(logging.WARNING):
        facts, risk = run(understand.understand_ticket(db_real, ticket, adapter=adapter))
    assert facts.order_nos == ["ORD20260115555"]
    assert facts.source == "rules"
    assert risk.level == "high"
    assert "degraded to rules-only" in caplog.text


def test_关掉模型通道时一次都不调用(db_real, monkeypatch):
    monkeypatch.setattr(settings, "TICKET_UNDERSTAND_LLM", False)

    class Boom:
        async def complete(self, **kwargs):
            raise AssertionError("关了模型通道还调它")

    ticket = _ticket(db_real, "ORD20260115555 要退款 900 元")
    facts, risk = run(understand.understand_ticket(db_real, ticket, adapter=Boom()))
    assert facts.source == "rules"
    assert risk.level == "high"
