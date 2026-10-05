"""接入层：渠道输入标准化、身份归一、重投递去重、建档与第一步轨迹。

用 ``db_real``：这里要测的是唯一约束、跨表写入与"要么全成要么全不成"的事务边界，
FakeDB 一样也测不出来。
"""
import json

import pytest

from config import settings
from models import CsCustomer, Ticket, TicketEvent
from services.clock import naive_now
from services.ticket.intake import (
    TicketIntake,
    TicketIntakeError,
    enabled_channels,
    normalize_attachments,
    normalize_content,
    normalize_email,
    normalize_phone,
    submit_ticket,
)


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    """钉住本文件依赖的非 bool 设置。

    ``_pin_feature_flags`` 只按类型钉 bool，而这几个是 str/int——本地 .env 里写了
    什么就会改变这里的行为，所以逐个钉回代码默认值。
    """
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat,email,app,api")
    monkeypatch.setattr(settings, "TICKET_INTAKE_MAX_CHARS", 6000)
    monkeypatch.setattr(settings, "TICKET_INTAKE_MAX_ATTACHMENTS", 8)
    monkeypatch.setattr(settings, "TICKET_SLA_HOURS", 24)


def _intake(**overrides) -> TicketIntake:
    base = {
        "channel": "web_chat",
        "content": "买的耳机三天了还没发货，订单号 ORD20260115001，帮我看看。",
    }
    base.update(overrides)
    return TicketIntake(**base)


def test_邮箱归一取显示名里的那个地址():
    assert normalize_email("  Zhang.San@Corp.COM ") == "zhang.san@corp.com"
    assert normalize_email("张三 <Zhang.San@corp.com>") == "zhang.san@corp.com"
    assert normalize_email("不是邮箱") is None
    assert normalize_email(None) is None


def test_电话归一只对中国移动号码削前缀():
    assert normalize_phone("+86 138-0013-8000") == "13800138000"
    assert normalize_phone("008613800138000") == "13800138000"
    assert normalize_phone("13800138000") == "13800138000"
    # 非中国号码原样留数字：猜国家码会让两个人匹配成同一个人
    assert normalize_phone("+1 (415) 555-0132") == "14155550132"
    assert normalize_phone("总机 转 203") == "203"
    assert normalize_phone(None) is None


def test_邮件去掉被引述的旧正文():
    raw = (
        "这次是第三次了，要求退款。\n"
        "\n"
        "> 原始邮件\n"
        "> 发件人: a@b.c\n"
        "> 上次那个订单我已经取消\n"
    )
    cleaned = normalize_content(raw, channel="email")
    assert cleaned == "这次是第三次了，要求退款。"
    # 同一份正文从网页聊天进来时不剥引述：聊天里客户自己粘的引用是内容的一部分
    assert "> 原始邮件" in normalize_content(raw, channel="web_chat")


def test_清洗后什么都不剩要报错而不是建空单():
    with pytest.raises(TicketIntakeError):
        normalize_content("> 全是引述\n> 没有新内容\n", channel="email")
    with pytest.raises(TicketIntakeError):
        normalize_content("   \n\t  \n", channel="web_chat")


def test_超限是报错不是静默截断(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_INTAKE_MAX_CHARS", 50)
    with pytest.raises(TicketIntakeError) as exc:
        normalize_content("订" * 60, channel="web_chat")
    assert "超过上限" in str(exc.value)


def test_附件数量超限要报错():
    with pytest.raises(TicketIntakeError):
        normalize_attachments([f"截图{i}.png" for i in range(9)])
    assert normalize_attachments(None) == ()
    assert normalize_attachments("a.pdf, ,b.pdf") == ("a.pdf", "b.pdf")


def test_控制字符不进正文():
    assert normalize_content("退款\x00问题\r\n第二行", channel="web_chat") == "退款问题\n第二行"


def test_渠道白名单取交集而不是照抄配置(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat,emial")
    assert enabled_channels() == frozenset({"web_chat"})


def test_未启用渠道与未知渠道分别报错(db_real, monkeypatch):
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat")
    with pytest.raises(TicketIntakeError) as exc:
        submit_ticket(db_real, _intake(channel="email"), workspace_id="w1")
    assert "未启用" in str(exc.value)

    with pytest.raises(TicketIntakeError) as exc:
        submit_ticket(db_real, _intake(channel="fax"), workspace_id="w1")
    assert "未知渠道" in str(exc.value)


def test_没有归属就不建单(db_real):
    with pytest.raises(TicketIntakeError):
        submit_ticket(db_real, _intake(), workspace_id="")


def test_建单写齐工单客户与第一步轨迹(db_real):
    result = submit_ticket(
        db_real,
        _intake(
            customer_email="Zhang.San@Corp.com",
            attachments=["照片.jpg"],
            subject=None,
        ),
        workspace_id="w1",
    )
    ticket = result.ticket
    assert result.created is True
    assert ticket.status == "new"
    # 还没走过风险节点，不是"评为低风险"
    assert ticket.risk_level is None
    assert ticket.sla_due_at is not None
    assert json.loads(ticket.attachments) == ["照片.jpg"]
    # 没有主题时用第一行顶上
    assert ticket.subject.startswith("买的耳机三天了还没发货")
    # 身份键在抽取之前就写进 entities：抽取挂了，"这是谁"仍然查得到
    entities = json.loads(ticket.entities)
    assert entities["identity"] == {"email": "zhang.san@corp.com"}

    events = db_real.query(TicketEvent).filter_by(ticket_id=ticket.id).all()
    assert len(events) == 1
    assert events[0].node == "intake"
    assert events[0].seq == 1

    customers = db_real.query(CsCustomer).filter_by(workspace_id="w1").all()
    assert len(customers) == 1
    assert customers[0].email == "zhang.san@corp.com"
    assert ticket.customer_id == customers[0].id


def test_没有身份键的工单不硬造一个客户(db_real):
    result = submit_ticket(db_real, _intake(), workspace_id="w1")
    assert result.customer_id is None
    assert db_real.query(CsCustomer).count() == 0


def test_同一封邮件重投递只建一张工单(db_real):
    first = submit_ticket(
        db_real, _intake(channel="email", external_ref=" <abc@mail> "), workspace_id="w1"
    )
    again = submit_ticket(
        db_real, _intake(channel="email", external_ref="<abc@mail>"), workspace_id="w1"
    )
    assert again.created is False
    assert again.deduped_against == first.ticket.id
    assert db_real.query(Ticket).count() == 1
    # 去重不补第二步轨迹：什么都没发生就不该在回放里多出一步
    assert db_real.query(TicketEvent).count() == 1


def test_身份键强度决定合并不靠猜(db_real):
    by_email = submit_ticket(
        db_real,
        _intake(channel="email", customer_email="li@corp.com"),
        workspace_id="w1",
    )
    by_phone = submit_ticket(
        db_real,
        _intake(channel="app", customer_phone="+86 138 0013 8000", content="还是那个订单"),
        workspace_id="w1",
    )
    # 两封都没有可关联的强键（external_ref），所以各自建一个客户是**正确**的：
    # 只有邮箱和只有电话，谁也不能证明是同一个人
    assert by_email.customer_id != by_phone.customer_id
    assert db_real.query(CsCustomer).count() == 2

    # 第三次同时给了邮箱，就必然并回第一个
    merged = submit_ticket(
        db_real,
        _intake(
            channel="web_chat",
            customer_email="LI@corp.com",
            customer_phone="13800138000",
        ),
        workspace_id="w1",
    )
    assert merged.customer_id == by_email.customer_id


def test_身份有多个候选时不画像并留痕(db_real):
    now = naive_now()
    db_real.add_all(
        [
            CsCustomer(
                id="c1", workspace_id="w1", email="dup@corp.com",
                created_at=now, updated_at=now,
            ),
            CsCustomer(
                id="c2", workspace_id="w1", email="dup@corp.com",
                created_at=now, updated_at=now,
            ),
        ]
    )
    db_real.commit()
    result = submit_ticket(
        db_real, _intake(channel="email", customer_email="dup@corp.com"), workspace_id="w1"
    )
    # 不自动合并：挑一行写画像是替客户决定"哪一行是真的"
    assert result.customer_id is None
    event = db_real.query(TicketEvent).filter_by(ticket_id=result.ticket.id).one()
    assert "多个候选" in event.message
