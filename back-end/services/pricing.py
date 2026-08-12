"""模型价目表与成本估算。

价格随时间、渠道、区域变动，把它硬编码进代码只会得到一个过期的谎言。
所以这里只做「可覆盖的估算」：命中价目表就算出成本，命中不了返回 None，
由上层显示「未知」。宁可承认不知道，也不要给一个看起来精确的假数字。

价目表放在 JSON 里（默认 back-end/model_prices.json，见同目录的
model_prices.example.json），格式：

    {
      "currency": "CNY",
      "models": {
        "glm-4.5-air": {"input_per_1m": 1.0, "output_per_1m": 1.0},
        "embedding-2": {"input_per_1m": 0.5, "output_per_1m": 0.0}
      }
    }

匹配规则是「精确优先、其次最长前缀」，这样 ``glm-4.5-air-0728`` 可以落到
``glm-4.5-air`` 的价格上，而不需要为每个日期后缀维护一行。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from config import settings

logger = logging.getLogger("pricing")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_BASE_DIR)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """每 100 万 token 的单价。"""

    input_per_1m: Decimal
    output_per_1m: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class Cost:
    amount: Decimal
    currency: str


class PriceTable:
    """从 JSON 加载的价目表。文件缺失或格式错误时表现为空表（成本未知）。"""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = prices or {}

    @property
    def empty(self) -> bool:
        return not self._prices

    @classmethod
    def load(cls, path: str) -> "PriceTable":
        resolved = path if os.path.isabs(path) else os.path.join(_BACKEND_DIR, path)
        if not os.path.exists(resolved):
            logger.info("No price table at %s; costs will be reported as unknown.", resolved)
            return cls()
        try:
            with open(resolved, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read price table: %s", type(exc).__name__)
            return cls()
        return cls(cls._parse(raw))

    @staticmethod
    def _parse(raw: Any) -> dict[str, ModelPrice]:
        if not isinstance(raw, dict):
            return {}
        default_currency = str(raw.get("currency") or "CNY")
        models = raw.get("models")
        if not isinstance(models, dict):
            return {}

        prices: dict[str, ModelPrice] = {}
        for model, entry in models.items():
            if not isinstance(entry, dict):
                continue
            try:
                prices[str(model)] = ModelPrice(
                    input_per_1m=Decimal(str(entry.get("input_per_1m", 0))),
                    output_per_1m=Decimal(str(entry.get("output_per_1m", 0))),
                    currency=str(entry.get("currency") or default_currency),
                )
            except (ArithmeticError, ValueError):
                logger.warning("Skipping malformed price entry for %s", model)
        return prices

    def lookup(self, model: str | None) -> ModelPrice | None:
        """精确匹配优先，否则取最长的前缀匹配。"""
        if not model or not self._prices:
            return None
        exact = self._prices.get(model)
        if exact is not None:
            return exact
        candidates = [key for key in self._prices if model.startswith(key)]
        if not candidates:
            return None
        return self._prices[max(candidates, key=len)]

    def estimate(
        self,
        model: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> Cost | None:
        price = self.lookup(model)
        if price is None:
            return None
        amount = (
            Decimal(prompt_tokens or 0) * price.input_per_1m
            + Decimal(completion_tokens or 0) * price.output_per_1m
        ) / Decimal(1_000_000)
        # 6 位小数足够表达单次调用的成本，又不至于在聚合时丢精度
        return Cost(amount=amount.quantize(Decimal("0.000001")), currency=price.currency)


_table: PriceTable | None = None


def price_table() -> PriceTable:
    global _table
    if _table is None:
        _table = PriceTable.load(settings.PRICING_CONFIG_PATH)
    return _table


def reset_price_table() -> None:
    """测试或改完价目表后强制重新加载。"""
    global _table
    _table = None


def estimate_cost(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> Cost | None:
    return price_table().estimate(model, prompt_tokens, completion_tokens)
