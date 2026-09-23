"""定价目录的数据访问层（docs/pricing.md §4.1）。

``find_policy`` 供运行时解析「当前生效价」；``find_policies`` 供启动校验「未来 3 天连续
覆盖」。只做读取原语，解析/计算/校验逻辑在 ``agent/pricing/service.py``。
"""

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.pricing import PricingPolicy


class PricingRepository(Protocol):
    async def find_policy(
        self, vendor: str, model: str, now: datetime
    ) -> tuple[Any, list[dict[str, Any]]] | None: ...

    async def find_policies(
        self, vendor: str, model: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, list[dict[str, Any]]]]: ...


class SqlAlchemyPricingRepository:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_policy(
        self, vendor: str, model: str, now: datetime
    ) -> tuple[Any, list[dict[str, Any]]] | None:
        """返回覆盖 ``now`` 的那条策略的 ``(policy_id, schedule)``，无则 None。"""
        async with self._session_factory() as session:
            stmt = (
                select(PricingPolicy)
                .where(
                    PricingPolicy.vendor == vendor,
                    PricingPolicy.model == model,
                    PricingPolicy.valid_from <= now,
                    PricingPolicy.valid_to > now,
                )
                .order_by(PricingPolicy.valid_from)
            )
            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                return None
            return (row.id, row.schedule)

    async def find_policies(
        self, vendor: str, model: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, list[dict[str, Any]]]]:
        """返回与 ``[start, end)`` 相交的所有策略（含 schedule），按起始升序。"""
        async with self._session_factory() as session:
            stmt = (
                select(PricingPolicy)
                .where(
                    PricingPolicy.vendor == vendor,
                    PricingPolicy.model == model,
                    PricingPolicy.valid_to > start,
                    PricingPolicy.valid_from < end,
                )
                .order_by(PricingPolicy.valid_from)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [(r.valid_from, r.valid_to, r.schedule) for r in rows]


class InMemoryPricingRepository:
    """内存版（测试用）：按 (vendor, model, 区间) 直接给策略。"""

    def __init__(self) -> None:
        self._policies: list[
            tuple[str, str, datetime, datetime, list[dict[str, Any]]]
        ] = []

    def add(
        self,
        vendor: str,
        model: str,
        valid_from: datetime,
        valid_to: datetime,
        schedule: list[dict[str, Any]],
    ) -> None:
        self._policies.append((vendor, model, valid_from, valid_to, schedule))

    async def find_policy(
        self, vendor: str, model: str, now: datetime
    ) -> tuple[Any, list[dict[str, Any]]] | None:
        for v, m, valid_from, valid_to, schedule in self._policies:
            if v == vendor and m == model and valid_from <= now < valid_to:
                return (id(schedule), schedule)
        return None

    async def find_policies(
        self, vendor: str, model: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, list[dict[str, Any]]]]:
        out: list[tuple[datetime, datetime, list[dict[str, Any]]]] = []
        for v, m, valid_from, valid_to, schedule in self._policies:
            if v == vendor and m == model and valid_to > start and valid_from < end:
                out.append((valid_from, valid_to, schedule))
        out.sort(key=lambda x: x[0])
        return out
