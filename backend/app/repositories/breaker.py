"""熔断状态（动作指纹）的数据访问层：SQLAlchemy + 内存实现。

熔断器状态是跨 run 的（工具 / 资源级），进程重启后据此续读失败计数，避免「崩溃后计数
归零、永远触发不了熔断」。只做存取原语（``load_all`` / ``save`` 幂等覆盖写），状态机在
``resilience/circuit_breaker.py`` 的 ``CircuitBreaker``——这里对应其 ``BreakerStore`` 协议。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.copilot import CopilotBreaker


class SqlAlchemyBreakerStore:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话（幂等覆盖写）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_all(self) -> dict[str, dict[str, Any]]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(CopilotBreaker.name, CopilotBreaker.state, CopilotBreaker.failures)
            )
            return {
                name: {"state": state, "failures": failures} for name, state, failures in rows.all()
            }

    async def save(self, name: str, state: str, failures: list[float]) -> None:
        stmt = pg_insert(CopilotBreaker).values(name=name, state=state, failures=failures)
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "state": stmt.excluded.state,
                "failures": stmt.excluded.failures,
                "updated_at": func.now(),
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()


class InMemoryBreakerStore:
    """内存版熔断状态（测试 / 无 DB 场景）：dict 幂等覆盖写。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    async def load_all(self) -> dict[str, dict[str, Any]]:
        return {name: dict(data) for name, data in self._rows.items()}

    async def save(self, name: str, state: str, failures: list[float]) -> None:
        self._rows[name] = {"state": state, "failures": list(failures)}
