"""实时计费定价包（见 docs/pricing.md）。

``strategy.py`` 放每厂商的计费函数（成本 = f(usage, 厂商自定义价格字段) -> 元），
``service.py`` 放价格解析/成本计算/启动校验。只有厂商「计费模式」变化才改这里，
「价格数值」变化只动 DB（``pricing_policy`` 表）。
"""

from app.agent.pricing.service import PriceQuote, PricingService
from app.agent.pricing.strategy import VENDOR_STRATEGIES, PricingError, PricingStrategy

__all__ = [
    "PricingError",
    "PricingService",
    "PriceQuote",
    "PricingStrategy",
    "VENDOR_STRATEGIES",
]
