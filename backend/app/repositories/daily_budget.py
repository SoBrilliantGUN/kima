"""Copilot 全局日预算的数据访问层：Protocol + SQLAlchemy 实现。

单日一行（day 为主键）记录成本与 token 累计，进程重启后据此续读，避免「重启清零」
绕过单日上限。只做存取原语，累计/滚动/预检逻辑在 `agent/runtime/budget.py` 的 `DailyBudget`。

`add` 是**原子累加**而非「读-改-写」：并发进程 flush 时用 ``INSERT ... ON CONFLICT DO
UPDATE SET x = x + EXCLUDED.x`` 合并增量，避免「后写覆盖先写」的竞态丢更新。
"""

from datetime import date
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.copilot import CopilotDailyBudget


class DailyBudgetRepository(Protocol):
    async def load(self, day: date) -> tuple[float, int] | None: ...
    async def add(self, day: date, cost_cny: float, tokens: int) -> None: ...


class SqlAlchemyDailyBudgetRepository:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话（原子累加）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self, day: date) -> tuple[float, int] | None:
        async with self._session_factory() as session:
            row = await session.get(CopilotDailyBudget, day)
            if row is None:
                return None
            return (row.cost_cny, row.tokens)

    async def add(self, day: date, cost_cny: float, tokens: int) -> None:
        """原子累加增量：day 不存在则插入、存在则 ``cost += Δ`` / ``tokens += Δ``。"""
        stmt = pg_insert(CopilotDailyBudget).values(
            day=day, cost_cny=cost_cny, tokens=tokens
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["day"],
            set_={
                "cost_cny": CopilotDailyBudget.cost_cny + stmt.excluded.cost_cny,
                "tokens": CopilotDailyBudget.tokens + stmt.excluded.tokens,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
