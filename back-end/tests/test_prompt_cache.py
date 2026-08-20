"""提示词缓存:命中量的读取、计价与前缀稳定性。

三件事各自会独立坏掉:

1. 提供商回传的 ``prompt_tokens_details.cached_tokens`` 读不到(字段缺失、
   是 dict 而不是对象),表现是面板上永远 0% 命中;
2. 命中量当成**额外**的输入去加,而不是从 ``prompt_tokens`` 里减掉再打折,
   表现是开了缓存反而显示更贵——正好把结论算反;
3. 系统提示词随预检索命中与否切换正文,而它是整个前缀的第一条消息,
   一变就是整段缓存作废。这一条最隐蔽:功能完全正常,只是缓存永远不命中。
"""
from __future__ import annotations

from decimal import Decimal

from services import pricing
from services.model_adapter import OpenAICompatibleAdapter
from services.prompt_library import get as get_prompt
from services.chat_service import ChatService


class _Details:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class _Usage:
    def __init__(self, prompt=0, completion=0, details=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        if details is not None:
            self.prompt_tokens_details = details


class _Span:
    """只记下 set_usage / set 收到了什么。"""

    def __init__(self):
        self.usage = None
        self.attributes = {}

    def set_usage(self, **kwargs):
        self.usage = kwargs

    def set(self, **kwargs):
        self.attributes.update({k: v for k, v in kwargs.items() if v is not None})


# ========== 读取命中量 ==========


def test_reads_cached_tokens_from_object():
    usage = _Usage(prompt=1200, completion=300, details=_Details(800))

    assert OpenAICompatibleAdapter._cached_tokens(usage) == 800


def test_reads_cached_tokens_from_dict():
    """某些 OpenAI 兼容实现不做模型化,details 直接是个 dict。"""
    usage = _Usage(prompt=100, details={"cached_tokens": 40})

    assert OpenAICompatibleAdapter._cached_tokens(usage) == 40


def test_missing_details_yields_none_not_zero():
    """"没有缓存信息"和"命中 0 个"是两件事,后者会把命中率的分母算错。"""
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(prompt=100)) is None
    assert OpenAICompatibleAdapter._cached_tokens(None) is None
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(details=None)) is None


def test_rejects_nonsense_cached_values():
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(details=_Details(None))) is None
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(details=_Details(-1))) is None
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(details=_Details("8"))) is None
    # bool 是 int 的子类,不能当成命中数
    assert OpenAICompatibleAdapter._cached_tokens(_Usage(details=_Details(True))) is None


def test_record_usage_sets_cached_and_ratio():
    span = _Span()
    usage = _Usage(prompt=1000, completion=100, details=_Details(600))

    OpenAICompatibleAdapter._record_usage(span, usage, [], "out", "glm-4.5-air")

    assert span.usage["cached_tokens"] == 600
    assert span.attributes["cache_hit_ratio"] == 0.6


def test_estimated_usage_reports_no_cache_info():
    """本地估算根本不知道提供商那边命中了什么,填 0 会被读成"一点没命中"。"""
    span = _Span()

    OpenAICompatibleAdapter._record_usage(span, None, [], "out", "glm-4.5-air")

    assert span.usage["cached_tokens"] is None
    assert "cache_hit_ratio" not in span.attributes


# ========== 计价 ==========


def _table() -> pricing.PriceTable:
    """两个模型:一个不写缓存价(走默认比例),一个显式写死。"""
    return pricing.PriceTable(
        pricing.PriceTable._parse(
            {
                "currency": "CNY",
                "models": {
                    "cheap": {"input_per_1m": 10.0, "output_per_1m": 20.0},
                    "explicit": {
                        "input_per_1m": 10.0,
                        "output_per_1m": 20.0,
                        "cached_input_per_1m": 1.0,
                    },
                },
            }
        )
    )


def test_cached_tokens_are_discounted_not_added():
    table = _table()

    plain = table.estimate("cheap", 1_000_000, 0, None)
    cached = table.estimate("cheap", 1_000_000, 0, 1_000_000)

    # 全部命中时按默认比例 0.5 计:10 → 5
    assert plain.amount == Decimal("10.000000")
    assert cached.amount == Decimal("5.000000")
    # 开了缓存不该变贵
    assert cached.amount < plain.amount


def test_partial_hit_splits_input():
    table = _table()

    cost = table.estimate("cheap", 1_000_000, 0, 400_000)

    # 600k 全价 + 400k 半价 = 6 + 2 = 8
    assert cost.amount == Decimal("8.000000")


def test_explicit_cached_rate_overrides_default_ratio():
    table = _table()

    cost = table.estimate("explicit", 1_000_000, 0, 1_000_000)

    assert cost.amount == Decimal("1.000000")


def test_cached_greater_than_prompt_is_clamped():
    """两个数偶尔会对不上;不夹取的话新鲜输入变成负数,成本会算成负的。"""
    table = _table()

    cost = table.estimate("cheap", 1_000, 0, 999_999)

    assert cost.amount >= 0


def test_negative_cached_is_ignored():
    table = _table()

    assert table.estimate("cheap", 1_000_000, 0, -5).amount == Decimal("10.000000")


def test_none_cached_behaves_like_before():
    """没有缓存信息时的计价必须和改动前逐位相同。"""
    table = _table()

    assert table.estimate("cheap", 1_000_000, 500_000, None).amount == Decimal(
        "20.000000"
    )


def test_unknown_model_still_returns_none():
    assert _table().estimate("nope", 100, 100, 50) is None


# ========== 前缀稳定性 ==========


def test_stable_prefix_keeps_system_prompt_identical(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_PREFIX", True)
    template = get_prompt("chat_system_rag", "v2")

    hit = ChatService._system_prompt(template, True)
    miss = ChatService._system_prompt(template, False)

    # 这是整个改动的要点:前缀第一条消息不再随预检索结果变化
    assert hit == miss
    assert "预先检索" not in hit


def test_disabling_stable_prefix_restores_old_behaviour(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_PREFIX", False)
    template = get_prompt("chat_system_rag", "v2")

    hit = ChatService._system_prompt(template, True)
    miss = ChatService._system_prompt(template, False)

    assert hit != miss
    assert "预先检索" in hit
    assert "预先检索" not in miss
