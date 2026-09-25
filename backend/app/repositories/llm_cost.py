"""调用级成本明细（``copilot_llm_cost``）的数据访问层（docs/pricing.md §4.4）。

每笔真实 LLM 调用一行，带「当时解析出的价格快照 + 成本（元）」，与 ``CopilotLLMSnapshot``
同 key ``(run_id, call_key)`` 幂等 upsert、不设 TTL（审计留痕）。缓存命中不写（不重复计费）。
"""

import uuid
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runtime.budget import Usage
from app.models.pricing import CopilotLLMCost


class CostStore(Protocol):
    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        vendor: str,
        model: str,
        pricing_policy_id: uuid.UUID | None,
        price_snapshot: dict[str, Any],
        usage: Usage,
        cost_cny: float,
    ) -> None: ...


class SqlAlchemyCostStore:
    """SQLAlchemy 实现：持有 session factory，幂等 upsert（与快照同 key 语义）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        vendor: str,
        model: str,
        pricing_policy_id: uuid.UUID | None,
        price_snapshot: dict[str, Any],
        usage: Usage,
        cost_cny: float,
    ) -> None:
        stmt = pg_insert(CopilotLLMCost).values(
            run_id=run_id,
            call_key=call_key,
            vendor=vendor,
            model=model,
            pricing_policy_id=pricing_policy_id,
            price_snapshot=price_snapshot,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_cny=cost_cny,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "call_key"],
            set_={
                "vendor": stmt.excluded.vendor,
                "model": stmt.excluded.model,
                "pricing_policy_id": stmt.excluded.pricing_policy_id,
                "price_snapshot": stmt.excluded.price_snapshot,
                "input_tokens": stmt.excluded.input_tokens,
                "output_tokens": stmt.excluded.output_tokens,
                "cache_read_tokens": stmt.excluded.cache_read_tokens,
                "cost_cny": stmt.excluded.cost_cny,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()


class InMemoryCostStore:
    """内存版（测试用）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        vendor: str,
        model: str,
        pricing_policy_id: uuid.UUID | None,
        price_snapshot: dict[str, Any],
        usage: Usage,
        cost_cny: float,
    ) -> None:
        self._rows[(run_id, call_key)] = {
            "vendor": vendor,
            "model": model,
            "pricing_policy_id": pricing_policy_id,
            "price_snapshot": price_snapshot,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cost_cny": cost_cny,
        }

    def get(self, run_id: str, call_key: str) -> dict[str, Any] | None:
        return self._rows.get((run_id, call_key))
