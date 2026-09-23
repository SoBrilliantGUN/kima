"""每厂商计费 strategy（docs/pricing.md D3）。

不同厂商定价策略不同（DeepSeek 三维分时、通义/智谱按输入长度阶梯、Kimi 还有缓存写入费），
故每厂商一个 strategy 类，各自解释 ``schedule`` 时段里的 ``prices`` 自定义字段、算出
**人民币成本（元）**。只有厂商「计费模式」变化才改/增这里，「价格数值」变化只改 DB。
"""

from typing import Any, Protocol

from app.agent.runtime.budget import Usage
from app.core.exceptions import DomainError


class PricingError(DomainError):
    """价格解析/计算失败：找不到生效价、厂商未注册、价格字段缺键等。fail-closed。"""

    code = "pricing_error"


class PricingStrategy(Protocol):
    """厂商计费函数：``compute_cost(usage, prices) -> 元``。prices 缺键/非法应抛 PricingError。"""

    def compute_cost(self, usage: Usage, prices: dict[str, Any]) -> float: ...


class DeepSeekStrategy:
    """DeepSeek 三维：input（非缓存）/ cache_hit（缓存命中）/ output，单位「元/百万 token」。

    billable_input = 输入 - 缓存命中；缓存命中按更低的 cache_hit 单价，其余输入按 input，
    输出按 output（对齐原来 budget.py 的 compute_cost 语义，只是币种换成元）。
    """

    required_keys = frozenset({"input", "cache_hit", "output"})

    def compute_cost(self, usage: Usage, prices: dict[str, Any]) -> float:
        billable_input = usage.input_tokens - usage.cache_read_tokens
        return (
            billable_input * _price(prices, "input")
            + usage.cache_read_tokens * _price(prices, "cache_hit")
            + usage.output_tokens * _price(prices, "output")
        ) / 1_000_000


class SiliconFlowStrategy:
    """SiliconFlow（embedding/rerank）单维：只按 input 计（元/百万 token）。"""

    required_keys = frozenset({"input"})

    def compute_cost(self, usage: Usage, prices: dict[str, Any]) -> float:
        return usage.input_tokens * _price(prices, "input") / 1_000_000


def _price(prices: dict[str, Any], key: str) -> float:
    try:
        return float(prices[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise PricingError(f"价格配置缺键或非法：{key!r}") from exc


VENDOR_STRATEGIES: dict[str, PricingStrategy] = {
    "deepseek": DeepSeekStrategy(),
    "siliconflow": SiliconFlowStrategy(),
}
