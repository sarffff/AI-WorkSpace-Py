"""治理层：一键暂停、每日退款额度、限额与默认值的合成。

用 ``db_real``：额度的正确性依赖于一次真实的 SUM 聚合与它读到的行，
FakeDB 上没有这些行为。
"""
from decimal import Decimal

import pytest

from config import settings
from models import CsOperation
from services.clock import naive_now
from services.ticket import governor


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_DAILY_REFUND_LIMIT", 1000.0)
    monkeypatch.setattr(settings, "TICKET_MAX_COST_PER_TICKET", 0.0)
    monkeypatch.setattr(settings, "TICKET_MAX_TOOL_CALLS", 12)


WS = "w1"


def _refund(db, order_no: str, amount: str, *, ticket_id: str, status: str = "executed", at=None):
    """直接写一笔账本，用于造"今天已经退了多少"。"""
    moment = at or naive_now()
    db.add(
        CsOperation(
            id=f"op-{order_no}",
            workspace_id=WS,
            ticket_id=ticket_id,
            tool_name="create_refund",
            operation="refund.create",
            permission="fund",
            idempotency_key=f"k-{order_no}",
            status=status,
            amount=Decimal(amount),
            currency="CNY",
            created_at=moment,
            completed_at=moment,
        )
    )


def test_没有governor行时用配置默认值而且不顺手建一行(db_real):
    limits = governor.effective_limits(db_real, WS)
    assert limits["paused"] is False
    assert limits["daily_refund_limit"] == Decimal("1000.0") or limits["daily_refund_limit"] == Decimal("1000")
    assert governor.state(db_real, WS) is None


def test_工作区覆盖优先而NULL落回默认(db_real):
    row = governor.get_or_create(db_real, WS)
    row.daily_refund_limit = Decimal("50")
    db_real.commit()
    assert governor.effective_limits(db_real, WS)["daily_refund_limit"] == Decimal("50")

    row.daily_refund_limit = None
    db_real.commit()
    assert governor.effective_limits(db_real, WS)["daily_refund_limit"] == Decimal(
        str(settings.TICKET_DAILY_REFUND_LIMIT)
    )


def test_当日额度按自然日而不是滚动窗口(db_real):
    from datetime import datetime

    _refund(db_real, "A1", "400", ticket_id="t1", at=datetime(2026, 1, 1, 23, 30))
    db_real.commit()
    # 昨天 23:30 那笔不算进今天
    assert governor.refund_used_today(db_real, WS) == Decimal("0")
    _refund(db_real, "A2", "300", ticket_id="t2")
    db_real.commit()
    assert governor.refund_used_today(db_real, WS) == Decimal("300")


def test_被拦和失败的账不占用额度(db_real):
    _refund(db_real, "A1", "900", ticket_id="t1", status="blocked")
    _refund(db_real, "A2", "900", ticket_id="t2", status="failed")
    db_real.commit()
    assert governor.refund_used_today(db_real, WS) == Decimal("0")


def test_待批的退款已经占用额度(db_real):
    # 人已经点头、只是还没执行：那是一笔已经承诺出去的钱。等执行完再算，
    # 等于允许同一天里堆二十笔待批退款
    _refund(db_real, "A1", "900", ticket_id="t1", status="pending_approval")
    db_real.commit()
    assert governor.refund_used_today(db_real, WS) == Decimal("900")


def test_查询类永远不被治理拦(db_real):
    governor.pause(db_real, WS, actor_id="u9", reason="演练")
    db_real.commit()
    assert governor.check_write(db_real, WS, permission="read").allowed is True


def test_暂停拦住修改类与资金类(db_real):
    governor.pause(db_real, WS, actor_id="u9", reason="支付网关故障，先停手")
    db_real.commit()
    for permission in ("mutate", "fund"):
        verdict = governor.check_write(db_real, WS, permission=permission, amount=Decimal("10"))
        assert not verdict.allowed and verdict.triggers == ("paused",)
        assert "支付网关故障" in verdict.reason
    governor.resume(db_real, WS, actor_id="u9")
    db_real.commit()
    assert governor.check_write(db_real, WS, permission="fund", amount=Decimal("10")).allowed is True


def test_不动钱的资金类动作不占额度(db_real):
    # 取消订单是资金类（要人批），但它退的款是零。把"金额没给"当成最坏情况
    # 会把它一律拦成额度不足——那条保守规则属于理解层和工具层，不属于这里
    row = governor.get_or_create(db_real, WS)
    row.daily_refund_limit = Decimal("100")
    db_real.commit()
    assert governor.check_write(db_real, WS, permission="fund", amount=None).allowed is True


def test_剩余额度不够这一笔就不放行(db_real):
    row = governor.get_or_create(db_real, WS)
    row.daily_refund_limit = Decimal("100")
    db_real.commit()
    assert governor.check_write(db_real, WS, permission="fund", amount=Decimal("80")).allowed is True
    assert governor.check_write(db_real, WS, permission="fund", amount=Decimal("120")).allowed is False


def test_暂停是被记录过的动作(db_real):
    row = governor.pause(db_real, WS, actor_id="u9", reason="线上错误操作率飙升")
    db_real.commit()
    assert row.paused is True
    assert row.pause_updated_by == "u9"
    assert row.pause_reason == "线上错误操作率飙升"
