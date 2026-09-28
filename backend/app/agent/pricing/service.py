"""价格解析 + 成本计算 + 启动校验（docs/pricing.md §4.2/§4.5）。

- ``resolve``：命中「生效区间 → 时段」返回 ``PriceQuote``，找不到生效价抛 ``PricingError``
  （运行时 fail-closed）。带短 TTL 内存缓存，避免每笔调用都打 DB。
- ``compute_cost``：按厂商 strategy 算成本（元）。
- ``validate_startup``：校验每个 (厂商, 模型) 未来 3 天连续覆盖 + 价格字段齐全，否则报错。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from app.agent.pricing.strategy import VENDOR_STRATEGIES, PricingError, PricingStrategy
from app.agent.runtime.budget import Usage
from app.repositories.pricing import PricingRepository


@dataclass(frozen=True)
class PriceQuote:
    """某时刻解析到的价格：策略 id + 命中的时段 + 厂商自定义单价字段。"""

    policy_id: uuid.UUID | None
    slot_start: str
    slot_end: str
    prices: dict[str, Any]


def _hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _slot_for(schedule: list[dict[str, Any]], t: time) -> dict[str, Any]:
    """命中时段：``[start, end)`` 半开区间；``end="24:00"`` 即 1440 分钟（日终，不含）。"""
    minutes = t.hour * 60 + t.minute
    for slot in schedule:
        start = _hhmm_to_minutes(slot["start"])
        end = _hhmm_to_minutes(slot["end"])
        if start <= minutes < end:
            return slot
    raise PricingError(f"时段未覆盖当前时刻 {t.strftime('%H:%M')}")


class PricingService:
    def __init__(self, repo: PricingRepository, *, cache_ttl_seconds: int = 60) -> None:
        self._repo = repo
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[datetime, PriceQuote]] = {}

    async def resolve(self, vendor: str, model: str, now: datetime) -> PriceQuote:
        """解析 ``now``（UTC）对 (vendor, model) 的生效价；带短 TTL 缓存。"""
        key = (vendor, model)
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]).total_seconds() < self._cache_ttl_seconds:
            return cached[1]
        quote = await self._resolve_uncached(vendor, model, now)
        self._cache[key] = (now, quote)
        return quote

    async def _resolve_uncached(self, vendor: str, model: str, now: datetime) -> PriceQuote:
        found = await self._repo.find_policy(vendor, model, now)
        if found is None:
            raise PricingError(f"无生效价格：vendor={vendor} model={model} at {now.isoformat()}")
        policy_id, schedule = found
        slot = _slot_for(schedule, now.time())
        return PriceQuote(
            policy_id=policy_id,
            slot_start=slot["start"],
            slot_end=slot["end"],
            prices=slot["prices"],
        )

    def compute_cost(self, vendor: str, usage: Usage, quote: PriceQuote) -> float:
        """按厂商 strategy 算成本（元）；厂商未注册/字段缺失抛 PricingError。"""
        strategy = VENDOR_STRATEGIES.get(vendor)
        if strategy is None:
            raise PricingError(f"厂商未注册 strategy：{vendor}")
        return strategy.compute_cost(usage, quote.prices)

    async def validate_startup(self, now: datetime, targets: list[tuple[str, str]]) -> None:
        """启动校验：每个 (vendor, model) 未来 3 天连续覆盖 + 价格字段齐全，否则抛错。

        窗口 ``[now, now+3d)`` 必须被策略区间无缝覆盖（非重叠已由 DB exclusion 约束保证），
        且每段 schedule 的每个时段 prices 都含该厂商 strategy 所需的键。
        """
        end = now + timedelta(days=3)
        for vendor, model in targets:
            strategy = VENDOR_STRATEGIES.get(vendor)
            if strategy is None:
                raise PricingError(f"厂商未注册 strategy：{vendor}")
            policies = await self._repo.find_policies(vendor, model, now, end)
            if not self._covers(policies, now, end):
                raise PricingError(
                    f"价格覆盖不足 3 天：vendor={vendor} model={model}"
                    f"（需连续覆盖至 {end.isoformat()}）"
                )
            self._check_schedules(vendor, strategy, policies)

    @staticmethod
    def _covers(
        policies: list[tuple[datetime, datetime, list[dict[str, Any]]]],
        start: datetime,
        end: datetime,
    ) -> bool:
        """区间（已按 valid_from 升序）是否无缝覆盖 [start, end)。"""
        if not policies:
            return False
        cursor = start
        for valid_from, valid_to, _ in policies:
            if valid_from > cursor:
                return False  # 有空洞
            if valid_to > cursor:
                cursor = valid_to
        return cursor >= end

    @staticmethod
    def _check_schedules(
        vendor: str,
        strategy: PricingStrategy,
        policies: list[tuple[datetime, datetime, list[dict[str, Any]]]],
    ) -> None:
        required = getattr(strategy, "required_keys", None)
        if not required:
            return
        for _, _, schedule in policies:
            for slot in schedule:
                missing = required - set(slot.get("prices", {}))
                if missing:
                    raise PricingError(f"价格字段缺失：vendor={vendor} 缺 {sorted(missing)}")
