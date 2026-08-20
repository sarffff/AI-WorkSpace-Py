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

``cached_input_per_1m`` 是可选的第三项:提供商上下文缓存命中的输入单价。不写就按
标准输入价打折估算(见 ``_DEFAULT_CACHED_RATIO``)。

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

# 价目表没给 ``cached_input_per_1m`` 时,缓存命中按标准输入价的这个比例计。
#
# 0.5 是智谱文档给的说法("通常为标准价格的 50%")。它是**估算的默认值**,不是
# 承诺:各家折扣不同(有的低到 0.1),同一家也会变。所以真正在意账单精度时应当
# 在 model_prices.json 里显式写死这一项,而不是依赖这个数。
#
# 为什么不干脆当成 0(缓存命中免费):那会把成本系统性低估,而低估比高估危险——
# 面板上看着便宜,账单上不是。宁可保守。
_DEFAULT_CACHED_RATIO = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """每 100 万 token 的单价。"""

    input_per_1m: Decimal
    output_per_1m: Decimal
    currency: str
    # 上下文缓存命中的输入单价。价目表没写就按 ``input_per_1m`` 的
    # ``_DEFAULT_CACHED_RATIO`` 折算——见该常量的说明。
    cached_input_per_1m: Decimal | None = None

    @property
    def cached_input(self) -> Decimal:
        if self.cached_input_per_1m is not None:
            return self.cached_input_per_1m
        return self.input_per_1m * _DEFAULT_CACHED_RATIO


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
                cached = entry.get("cached_input_per_1m")
                prices[str(model)] = ModelPrice(
                    input_per_1m=Decimal(str(entry.get("input_per_1m", 0))),
                    output_per_1m=Decimal(str(entry.get("output_per_1m", 0))),
                    currency=str(entry.get("currency") or default_currency),
                    cached_input_per_1m=(
                        Decimal(str(cached)) if cached is not None else None
                    ),
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
        cached_tokens: int | None = None,
    ) -> Cost | None:
        """算一次调用的成本。

        ``cached_tokens`` 是 ``prompt_tokens`` 的**子集**(提供商上下文缓存命中的
        那部分),所以要先从输入里减掉,再按打折价单独计一遍。当成额外的量去加
        会让开了缓存的调用看起来比没开更贵——正好把结论算反。

        夹取到 ``[0, prompt_tokens]``:提供商回传的两个数偶尔会对不上(不同分片
        统计),而一个大于输入总量的缓存命中会让新鲜输入变成负数,把成本算成负的。
        """
        price = self.lookup(model)
        if price is None:
            return None
        prompt = prompt_tokens or 0
        cached = min(max(cached_tokens or 0, 0), prompt)
        amount = (
            Decimal(prompt - cached) * price.input_per_1m
            + Decimal(cached) * price.cached_input
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
    cached_tokens: int | None = None,
) -> Cost | None:
    return price_table().estimate(
        model, prompt_tokens, completion_tokens, cached_tokens
    )
