"""LLM 调用级快照的 Postgres 实现（Protocol 见 `agent/snapshot.py` 的 `SnapshotStore`）。

每个 (run_id, call_key) 一行，``put`` 幂等覆盖写；``get`` 返回 output JSON 字典或 None。
清理（TTL）由 worker 或惰性删除负责，见 ``docs/llm-gateway.md`` §5/§7。
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.copilot import CopilotLLMSnapshot


class SqlAlchemySnapshotStore:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, run_id: str, call_key: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(CopilotLLMSnapshot, (run_id, call_key))
            return row.output if row is not None else None

    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        kind: str,
        output: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> None:
        # 原子 upsert：并发写同一 (run_id, call_key) 时「读-改-写」会丢更新，
        # 用 ON CONFLICT DO UPDATE 交给 DB 原子合并（at-least-once，后写覆盖同键）。
        stmt = pg_insert(CopilotLLMSnapshot).values(
            run_id=run_id, call_key=call_key, kind=kind, output=output, usage=usage
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "call_key"],
            set_={"kind": kind, "output": output, "usage": usage},
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def delete_older_than(self, cutoff: datetime) -> int:
        """删除 ``created_at`` 早于 ``cutoff`` 的快照（TTL 清理），返回删除条数。"""
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CopilotLLMSnapshot).where(CopilotLLMSnapshot.created_at < cutoff)
            )
            await session.commit()
            return cast(Any, result).rowcount or 0
