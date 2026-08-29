"""用量闸门的测试。

挑的都是「实现里做过一个具体决定」的地方，而不是把每个分支走一遍：

- 滑动窗口 vs 固定窗口（边界上会不会放过两倍的量）
- 被拦的请求不计数（否则持续重试等于自我永久封禁）
- 未定价模型的 cost 是 NULL（只卡成本会被绕过，token 是兜底）
- 混币种不做汇率换算
- 配额查询失败要放行（护栏不是鉴权）
- 频率闸门在查库之前（限流不该把数据库一起打满）
"""
from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import pytest

from config import settings
from models import TraceSpan
from services import usage_guard
from services.clock import naive_now


@pytest.fixture(autouse=True)
def _clean_counter():
    usage_guard.reset_rate_counter()
    yield
    usage_guard.reset_rate_counter()


@pytest.fixture
def guard_on(monkeypatch):
    """打开闸门，各项上限由每个测试自己设。"""
    monkeypatch.setattr(settings, "USAGE_GUARD_ENABLED", True)
    monkeypatch.setattr(settings, "USAGE_RATE_WINDOW_MINUTES", 1.0)
    monkeypatch.setattr(settings, "USAGE_RATE_MAX_REQUESTS", 0)
    monkeypatch.setattr(settings, "USAGE_QUOTA_WINDOW_HOURS", 24.0)
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 0.0)
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_TOKENS", 0)
    return settings


def _span(
    db,
    *,
    user_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost=None,
    currency: str | None = "CNY",
    age_hours: float = 0.0,
    span_id: str | None = None,
) -> TraceSpan:
    import uuid

    span = TraceSpan(
        id=span_id or uuid.uuid4().hex,
        trace_id=uuid.uuid4().hex,
        name="llm.chat",
        kind="llm",
        user_id=user_id,
        started_at=naive_now() - timedelta(hours=age_hours),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=Decimal(str(cost)) if cost is not None else None,
        currency=currency if cost is not None else None,
    )
    db.add(span)
    db.commit()
    return span


# ---- 开关 ----------------------------------------------------------------


def test_关闭时一律放行(db_real, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_GUARD_ENABLED", False)
    monkeypatch.setattr(settings, "USAGE_RATE_MAX_REQUESTS", 1)
    for _ in range(10):
        assert usage_guard.check(db_real, "u1") is None


def test_三项上限都为零时放行(db_real, guard_on):
    for _ in range(10):
        assert usage_guard.check(db_real, "u1") is None


# ---- 频率 ----------------------------------------------------------------


def test_频率上限第n加一次才拦(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_RATE_MAX_REQUESTS", 3)
    assert usage_guard.check(db_real, "u1") is None
    assert usage_guard.check(db_real, "u1") is None
    assert usage_guard.check(db_real, "u1") is None
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None
    assert rejection.kind == "rate"
    assert rejection.retry_after is not None and rejection.retry_after >= 1


def test_频率按用户隔离(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_RATE_MAX_REQUESTS", 1)
    assert usage_guard.check(db_real, "u1") is None
    assert usage_guard.check(db_real, "u1") is not None
    # 另一个用户不受影响——按 IP 限流会把这两个人算成一个，这条就是钉住那个区别
    assert usage_guard.check(db_real, "u2") is None


def test_被拦的请求不计数(guard_on):
    """被拦的请求如果也计数，持续重试的客户端会把窗口无限往后推。

    表现是「等够了时间也还是 429」，而日志里看不出为什么——每次重试都在悄悄
    延长自己的窗口，等于把限流变成永久封禁。
    """
    clock = _FakeClock()
    counter = usage_guard._RateCounter(clock=clock)
    kw = {"window_seconds": 10, "limit": 1}
    assert counter.hit("u", **kw) is None  # t=0
    # 窗口内连撞 5 次，每次都间隔 1 秒
    for _ in range(5):
        clock.advance(1)
        assert counter.hit("u", **kw) is not None
    # t=5。唯一被计数的那次在 t=0，窗口 10 秒 → t=11 必须放行。
    # 若把被拦的也计入，最后一次在 t=5，就要等到 t=15。
    clock.advance(6)  # t=11
    assert counter.hit("u", **kw) is None


class _FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_窗口是滑动的位置一个一个放出来(guard_on):
    """固定窗口与滑动窗口的区别只在时间推进之后才显现。

    固定窗口在翻页那一刻把额度**整份**重置，于是「窗口末尾打满 + 下个窗口开头
    再打满」可以在一个窗口长度内放过两倍的量。滑动窗口是一个一个地放出位置。

    这里 window=10、limit=2：
      t=0    A 通过
      t=5    B 通过        → 满
      t=6    拦下
      t=11   A 已滑出、B 还在 → **只该放出一个位置**
             C 通过
             再来一次必须拦（B 与 C 都在窗口内）
    固定窗口实现在 t=10 翻页时会给出两个位置，最后那一次断言就会失败。
    """
    clock = _FakeClock()
    counter = usage_guard._RateCounter(clock=clock)
    kw = {"window_seconds": 10, "limit": 2}

    assert counter.hit("u", **kw) is None  # A @ t=0
    clock.advance(5)
    assert counter.hit("u", **kw) is None  # B @ t=5
    clock.advance(1)
    assert counter.hit("u", **kw) is not None  # 满
    clock.advance(5)  # t=11：A 滑出，B 仍在
    assert counter.hit("u", **kw) is None  # C 拿到那一个位置
    assert counter.hit("u", **kw) is not None  # 没有第二个位置


def test_建议等待时间指向最早那次滑出窗口(guard_on):
    clock = _FakeClock()
    counter = usage_guard._RateCounter(clock=clock)
    kw = {"window_seconds": 10, "limit": 1}
    assert counter.hit("u", **kw) is None
    clock.advance(4)
    # 最早那次在 t=0，窗口 10 秒，所以还要等约 6 秒
    wait = counter.hit("u", **kw)
    assert wait is not None and 6 <= wait <= 7


def test_计数表会自动清理不会无限增长(guard_on):
    """``_hits`` 随「历史上出现过的用户数」单调增长，必须自己清。

    这条钉住的是「清理真的会发生」——一个定义了但从来没被调用的清理函数，
    和没有清理是一回事（正是这轮要修掉的 AGENT_APPROVAL_TIMEOUT_HOURS 的形状）。
    """
    clock = _FakeClock()
    counter = usage_guard._RateCounter(clock=clock)
    # 每个用户各打一次，全部落在窗口内
    for i in range(usage_guard._RateCounter._PRUNE_EVERY):
        counter.hit(f"u{i}", window_seconds=10, limit=5)
    # 都还在窗口内，所以清不掉
    assert len(counter._hits) == usage_guard._RateCounter._PRUNE_EVERY

    # 时间推过窗口，再打满一轮触发次数
    clock.advance(11)
    for i in range(usage_guard._RateCounter._PRUNE_EVERY):
        counter.hit(f"v{i}", window_seconds=10, limit=5)
    # 老的那批已经被顺带清掉，只剩新的
    assert all(key.startswith("v") for key in counter._hits)
    assert len(counter._hits) <= usage_guard._RateCounter._PRUNE_EVERY


def test_清理丢掉窗口外的用户(guard_on):
    clock = _FakeClock()
    counter = usage_guard._RateCounter(clock=clock)
    counter.hit("u1", window_seconds=10, limit=5)
    assert counter.prune(window_seconds=10) == 0
    clock.advance(11)
    assert counter.prune(window_seconds=10) == 1
    assert counter.prune(window_seconds=10) == 0


# ---- 成本 ----------------------------------------------------------------


def test_成本达到上限即拦(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    _span(db_real, user_id="u1", cost="0.4")
    assert usage_guard.check(db_real, "u1") is None
    _span(db_real, user_id="u1", cost="0.6")
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None
    assert rejection.kind == "cost"
    # 成本那道闸门给不出可信的等待时间（窗口是小时量级）
    assert rejection.retry_after is None


def test_窗口外的成本不算(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    monkeypatch.setattr(settings, "USAGE_QUOTA_WINDOW_HOURS", 24.0)
    _span(db_real, user_id="u1", cost="5.0", age_hours=25)
    assert usage_guard.check(db_real, "u1") is None


def test_成本按用户隔离(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    _span(db_real, user_id="u1", cost="2.0")
    assert usage_guard.check(db_real, "u1") is not None
    assert usage_guard.check(db_real, "u2") is None


def test_混币种各自独立比较(db_real, guard_on, monkeypatch):
    """不做汇率换算：项目里没有汇率来源，编一个只会得到假数字。

    0.6 CNY + 0.6 USD 在任何一种币种下都没到 1.0，所以放行。
    换算成同一币种再相加的实现会在这里拦下来。
    """
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    _span(db_real, user_id="u1", cost="0.6", currency="CNY")
    _span(db_real, user_id="u1", cost="0.6", currency="USD")
    assert usage_guard.check(db_real, "u1") is None
    # 单一币种超了才拦
    _span(db_real, user_id="u1", cost="0.5", currency="USD")
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None
    assert "USD" in rejection.message


# ---- token 兜底 ----------------------------------------------------------


def test_未定价模型只有token能拦住(db_real, guard_on, monkeypatch):
    """价目表里没有的模型 cost 是 NULL，只卡成本等于留了个后门。

    这条是 token 上限存在的全部理由：换一个未定价的模型，成本永远是 0。
    """
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    for _ in range(5):
        _span(db_real, user_id="u1", prompt_tokens=100_000, cost=None)
    # 成本闸门看不见这些调用
    assert usage_guard.check(db_real, "u1") is None

    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_TOKENS", 400_000)
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None
    assert rejection.kind == "tokens"


def test_token统计含输入与输出(db_real, guard_on, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_TOKENS", 100)
    _span(db_real, user_id="u1", prompt_tokens=60, completion_tokens=0)
    assert usage_guard.check(db_real, "u1") is None
    _span(db_real, user_id="u1", prompt_tokens=0, completion_tokens=40)
    assert usage_guard.check(db_real, "u1") is not None


def test_没有任何span时不报错(db_real, guard_on, monkeypatch):
    """sum() 在空集上返回 NULL 而不是 0，少了 coalesce 这里会抛 TypeError。"""
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_TOKENS", 100)
    assert usage_guard.check(db_real, "u-never-seen") is None
    spent = usage_guard.spent_in_window(db_real, "u-never-seen")
    assert spent.tokens == 0
    assert spent.costs == {}


# ---- 失效模式 ------------------------------------------------------------


def test_配额查询失败时放行(db_real, guard_on, monkeypatch):
    """这是护栏，不是鉴权。埋点表抖一下不该让全站对话都不可用。"""
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("trace_spans unavailable")

    monkeypatch.setattr(usage_guard, "spent_in_window", _boom)
    assert usage_guard.check(db_real, "u1") is None


def test_频率闸门在查库之前(db_real, guard_on, monkeypatch):
    """被限流的请求不该再去查一次聚合——那会让限流本身成为放大器。"""
    monkeypatch.setattr(settings, "USAGE_RATE_MAX_REQUESTS", 1)
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    calls: list[str] = []

    real = usage_guard.spent_in_window

    def _counting(db, user_id):
        calls.append(user_id)
        return real(db, user_id)

    monkeypatch.setattr(usage_guard, "spent_in_window", _counting)

    assert usage_guard.check(db_real, "u1") is None
    assert calls == ["u1"]
    # 第二次撞频率上限，不该再查库
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None and rejection.kind == "rate"
    assert calls == ["u1"]


def test_币种为空时不打印None(db_real, guard_on, monkeypatch):
    """currency 允许为 NULL；归到显式的 '?' 桶，而不是让 None 变成字典键。"""
    monkeypatch.setattr(settings, "USAGE_QUOTA_MAX_COST", 1.0)
    _span(db_real, user_id="u1", cost="2.0", currency=None)
    rejection = usage_guard.check(db_real, "u1")
    assert rejection is not None
    assert "None" not in rejection.message
