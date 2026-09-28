"""实时计费单测：厂商 strategy 成本计算、价格解析（时段命中）、启动 3 天覆盖校验。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.pricing import PriceQuote, PricingError, PricingService
from app.agent.pricing.strategy import DeepSeekStrategy, SiliconFlowStrategy
from app.agent.runtime.budget import Usage
from app.repositories.pricing import InMemoryPricingRepository

_DEEPSEEK_DAY = [
    {
        "start": "00:00",
        "end": "08:00",
        "prices": {"input": 1.0, "cache_hit": 0.5, "output": 2.0},
    },
    {
        "start": "08:00",
        "end": "24:00",
        "prices": {"input": 3.0, "cache_hit": 1.5, "output": 6.0},
    },
]

_DEEPSEEK_FULL_DAY = [
    {
        "start": "00:00",
        "end": "24:00",
        "prices": {"input": 1.0, "cache_hit": 0.5, "output": 2.0},
    },
]


def _policy(
    repo: InMemoryPricingRepository,
    vendor: str,
    model: str,
    start: datetime,
    end: datetime,
) -> None:
    repo.add(vendor, model, start, end, _DEEPSEEK_DAY)


def test_deepseek_strategy_computes_cost() -> None:
    strategy = DeepSeekStrategy()
    prices = {"input": 1.0, "cache_hit": 0.5, "output": 2.0}
    # 无缓存命中：1M 输入 + 1M 输出 = 1.0 + 2.0 = 3.0 元
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert strategy.compute_cost(usage, prices) == pytest.approx(3.0)
    # 含缓存命中：800K 非缓存输入 + 200K 缓存 + 500K 输出
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=200_000)
    assert strategy.compute_cost(usage, prices) == pytest.approx(1.9)


def test_siliconflow_strategy_single_dimension() -> None:
    strategy = SiliconFlowStrategy()
    usage = Usage(input_tokens=1_000_000)
    assert strategy.compute_cost(usage, {"input": 0.5}) == pytest.approx(0.5)


def test_strategy_missing_key_raises() -> None:
    strategy = DeepSeekStrategy()
    with pytest.raises(PricingError):
        strategy.compute_cost(Usage(input_tokens=100), {"input": 1.0})  # 缺 cache_hit/output


async def test_resolve_hits_time_slot() -> None:
    repo = InMemoryPricingRepository()
    now = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)  # 04:00 UTC → 00:00-08:00 时段
    _policy(repo, "deepseek", "deepseek-chat", now - timedelta(days=1), now + timedelta(days=5))
    service = PricingService(repo, cache_ttl_seconds=0)

    quote = await service.resolve("deepseek", "deepseek-chat", now)
    assert quote.slot_start == "00:00"
    assert quote.slot_end == "08:00"
    assert quote.prices["input"] == 1.0

    evening = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)  # 12:00 → 08:00-24:00 时段
    quote2 = await service.resolve("deepseek", "deepseek-chat", evening)
    assert quote2.slot_start == "08:00"
    assert quote2.prices["input"] == 3.0


async def test_resolve_no_active_policy_raises() -> None:
    repo = InMemoryPricingRepository()
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    service = PricingService(repo, cache_ttl_seconds=0)
    with pytest.raises(PricingError):
        await service.resolve("deepseek", "deepseek-chat", now)


def test_compute_cost_unknown_vendor_raises() -> None:
    service = PricingService(InMemoryPricingRepository(), cache_ttl_seconds=0)
    q = PriceQuote(policy_id=None, slot_start="00:00", slot_end="24:00", prices={"input": 1.0})
    with pytest.raises(PricingError):
        service.compute_cost("unknown-vendor", Usage(input_tokens=1), q)


async def test_validate_startup_covers_three_days() -> None:
    repo = InMemoryPricingRepository()
    now = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    _policy(repo, "deepseek", "deepseek-chat", now - timedelta(days=1), now + timedelta(days=5))
    service = PricingService(repo, cache_ttl_seconds=0)
    await service.validate_startup(now, [("deepseek", "deepseek-chat")])  # 不抛即通过


async def test_validate_startup_rejects_gap() -> None:
    repo = InMemoryPricingRepository()
    now = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    # [now, now+1d] 与 [now+2d, now+5d] 之间有 1 天空洞
    repo.add(
        "deepseek",
        "deepseek-chat",
        now - timedelta(days=1),
        now + timedelta(days=1),
        _DEEPSEEK_FULL_DAY,
    )
    repo.add(
        "deepseek",
        "deepseek-chat",
        now + timedelta(days=2),
        now + timedelta(days=5),
        _DEEPSEEK_FULL_DAY,
    )
    service = PricingService(repo, cache_ttl_seconds=0)
    with pytest.raises(PricingError):
        await service.validate_startup(now, [("deepseek", "deepseek-chat")])


async def test_validate_startup_rejects_missing_keys() -> None:
    repo = InMemoryPricingRepository()
    now = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    repo.add(
        "deepseek",
        "deepseek-chat",
        now,
        now + timedelta(days=5),
        [{"start": "00:00", "end": "24:00", "prices": {"input": 1.0}}],  # 缺 cache_hit/output
    )
    service = PricingService(repo, cache_ttl_seconds=0)
    with pytest.raises(PricingError):
        await service.validate_startup(now, [("deepseek", "deepseek-chat")])
