"""价目表与成本估算。核心是「不知道就说不知道」。"""
from __future__ import annotations

import json
from decimal import Decimal

from services.pricing import PriceTable, reset_price_table


def _table(models: dict, currency: str = "CNY") -> PriceTable:
    return PriceTable(PriceTable._parse({"currency": currency, "models": models}))


def test_exact_match_wins():
    table = _table(
        {
            "glm-4.5": {"input_per_1m": 1, "output_per_1m": 2},
            "glm-4.5-air": {"input_per_1m": 0.5, "output_per_1m": 0.5},
        }
    )
    assert table.lookup("glm-4.5-air").input_per_1m == Decimal("0.5")


def test_longest_prefix_match_when_no_exact_entry():
    """带日期后缀的模型名不该逼着我们为每个版本维护一行。"""
    table = _table(
        {
            "glm": {"input_per_1m": 9, "output_per_1m": 9},
            "glm-4.5-air": {"input_per_1m": 1, "output_per_1m": 2},
        }
    )
    assert table.lookup("glm-4.5-air-0728").input_per_1m == Decimal("1")


def test_unknown_model_has_unknown_cost():
    """返回 None 表示未知；返回 0 会被误读成"免费"。"""
    table = _table({"glm-4.5": {"input_per_1m": 1, "output_per_1m": 2}})
    assert table.estimate("gpt-4o", 1000, 1000) is None


def test_cost_is_computed_per_million_tokens():
    table = _table({"m": {"input_per_1m": 3, "output_per_1m": 12}})
    cost = table.estimate("m", 2_000_000, 500_000)
    assert cost.amount == Decimal("12.000000")
    assert cost.currency == "CNY"


def test_missing_token_counts_are_treated_as_zero():
    table = _table({"m": {"input_per_1m": 3, "output_per_1m": 12}})
    assert table.estimate("m", None, None).amount == Decimal("0.000000")


def test_per_model_currency_overrides_default():
    table = _table(
        {
            "glm": {"input_per_1m": 1, "output_per_1m": 1},
            "gpt-4o": {"input_per_1m": 2, "output_per_1m": 8, "currency": "USD"},
        }
    )
    assert table.estimate("glm", 1_000_000, 0).currency == "CNY"
    assert table.estimate("gpt-4o", 1_000_000, 0).currency == "USD"


def test_malformed_entries_are_skipped_not_fatal():
    table = _table({"good": {"input_per_1m": 1, "output_per_1m": 1}, "bad": "oops"})
    assert table.lookup("good") is not None
    assert table.lookup("bad") is None


def test_missing_file_yields_empty_table(tmp_path):
    table = PriceTable.load(str(tmp_path / "nope.json"))
    assert table.empty
    assert table.estimate("anything", 100, 100) is None


def test_invalid_json_yields_empty_table(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text("{not json", encoding="utf-8")
    assert PriceTable.load(str(path)).empty


def test_load_from_file(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps({"currency": "USD", "models": {"m": {"input_per_1m": 1, "output_per_1m": 3}}}),
        encoding="utf-8",
    )
    table = PriceTable.load(str(path))
    assert not table.empty
    cost = table.estimate("m", 1_000_000, 1_000_000)
    assert cost.amount == Decimal("4.000000")
    assert cost.currency == "USD"


def test_reset_clears_cached_table():
    reset_price_table()
    from services import pricing

    assert pricing._table is None
