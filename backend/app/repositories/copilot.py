"""Copilot 记忆与事件日志的数据访问层：Protocol + SQLAlchemy 实现。

记忆仓库只做「存取原语」，召回/冲突/衰减/淘汰等策略逻辑放在 `services/copilot.py`。
`search` 走 pgvector 余弦距离；`list_active` 供 constraint 硬召回与容量淘汰。
事件仓库 append-only：写一条、按 run 顺序读。
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import column, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import CopilotEvent, CopilotMemory, MemoryKind


class CopilotMemoryRepository(Protocol):
    async def add(self, memory: CopilotMemory) -> CopilotMemory: ...
    async def get(self, memory_id: uuid.UUID) -> CopilotMemory | None: ...
    async def get_many(self, memory_ids: list[uuid.UUID]) -> list[CopilotMemory]: ...
    async def update(self, memory: CopilotMemory) -> CopilotMemory: ...
    async def delete_superseded_older_than(self, cutoff: datetime) -> int: ...
    async def search(
        self, kind: MemoryKind, query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]: ...
    async def search_lexical(
        self, kind: MemoryKind, query: str, top_k: int
    ) -> list[CopilotMemory]: ...
    async def search_cross_kind(
        self, kinds: list[MemoryKind], query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]: ...
    async def search_recoverable(
        self,
        kinds: list[MemoryKind],
        query_vec: list[float],
        similarity_threshold: float,
        now: datetime,
        window: timedelta,
        top_k: int,
    ) -> list[CopilotMemory]: ...
    async def list_active(self, kind: MemoryKind) -> list[CopilotMemory]: ...
    async def get_by_entity(self, kind: MemoryKind, entity_id: str) -> CopilotMemory | None: ...
    async def overwrite_entity(
        self,
        memory_id: uuid.UUID,
        content: str,
        embedding: list[float],
        trigger_conditions: dict[str, Any] | None,
    ) -> CopilotMemory: ...
    async def count_active(self, kind: MemoryKind) -> int: ...
    async def touch(self, memory_ids: list[uuid.UUID], now: datetime) -> None: ...


class CopilotEventRepository(Protocol):
    async def add_event(self, event: CopilotEvent) -> CopilotEvent: ...
    async def list_events(self, run_id: uuid.UUID) -> list[CopilotEvent]: ...


class SqlAlchemyCopilotMemoryRepository:
    """SQLAlchemy 实现，持有 AsyncSession，写操作在其方法内提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, memory: CopilotMemory) -> CopilotMemory:
        self._session.add(memory)
        try:
            await self._session.commit()
        except BaseException:
            # commit 失败（如并发同实体的唯一索引冲突）必须回滚，否则 session 留在
            # 「pending rollback」状态，拖垮同一请求里后续所有 DB 操作（「冲突即失败」
            # 应干净地失败，而不是毒化整个会话）。
            await self._session.rollback()
            raise
        await self._session.refresh(memory)
        return memory

    async def get(self, memory_id: uuid.UUID) -> CopilotMemory | None:
        return await self._session.get(CopilotMemory, memory_id)

    async def get_many(self, memory_ids: list[uuid.UUID]) -> list[CopilotMemory]:
        if not memory_ids:
            return []
        rows = await self._session.scalars(
            select(CopilotMemory).where(CopilotMemory.id.in_(memory_ids))
        )
        return list(rows)

    async def update(self, memory: CopilotMemory) -> CopilotMemory:
        await self._session.commit()
        await self._session.refresh(memory)
        return memory

    async def delete_superseded_older_than(self, cutoff: datetime) -> int:
        """硬删除软删除窗口已过期的 superseded 记忆（窗口结束后真删、找不回）。"""
        result = await self._session.execute(
            delete(CopilotMemory).where(
                CopilotMemory.superseded.is_(True),
                CopilotMemory.superseded_at.is_not(None),
                CopilotMemory.superseded_at < cutoff,
            )
        )
        await self._session.commit()
        return cast(Any, result).rowcount or 0

    async def search(
        self, kind: MemoryKind, query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]:
        """同 kind 内余弦相似 top-k（跳过 superseded 与无 embedding 的条目）。"""
        distance = CopilotMemory.embedding.cosine_distance(query_vec)
        rows = await self._session.scalars(
            select(CopilotMemory)
            .where(
                CopilotMemory.kind == kind,
                CopilotMemory.superseded.is_(False),
                CopilotMemory.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(top_k)
        )
        return list(rows)

    async def search_lexical(self, kind: MemoryKind, query: str, top_k: int) -> list[CopilotMemory]:
        """同 kind 内词法 top-k（BM25，`tsv` 生成列 + pg_jieba 分词）。

        命中「表名/错误码/专有名词」这类向量相似度漏掉的精确串；与 `search`（dense）
        在服务层 RRF 融合。跳过 superseded 条目。`tsv` 是迁移 0004 的生成列、未映射到
        ORM，故用 `column("tsv")` 引用（仿 `app/rag/lexical.py` 的裸列写法）。
        """
        tsv: Any = column("tsv")
        tsquery = func.plainto_tsquery("jiebacfg", query)
        rows = await self._session.scalars(
            select(CopilotMemory)
            .where(
                CopilotMemory.kind == kind,
                CopilotMemory.superseded.is_(False),
                tsv.op("@@")(tsquery),
            )
            .order_by(func.ts_rank(tsv, tsquery).desc())
            .limit(top_k)
        )
        return list(rows)

    async def search_cross_kind(
        self, kinds: list[MemoryKind], query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]:
        """跨 kind 余弦相似 top-k（跳过 superseded 与无 embedding）。

        冲突是局部的——新记忆只和语义上最相近的旧记忆冲突，跨型也如此（偏好↔情节）。
        约束是红线孤岛，不在候选集里；其余三型互为候选。
        """
        distance = CopilotMemory.embedding.cosine_distance(query_vec)
        rows = await self._session.scalars(
            select(CopilotMemory)
            .where(
                CopilotMemory.kind.in_(kinds),
                CopilotMemory.superseded.is_(False),
                CopilotMemory.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(top_k)
        )
        return list(rows)

    async def search_recoverable(
        self,
        kinds: list[MemoryKind],
        query_vec: list[float],
        similarity_threshold: float,
        now: datetime,
        window: timedelta,
        top_k: int,
    ) -> list[CopilotMemory]:
        """软删除窗口内可复活的 superseded 记忆：窗口期内 + 与 query 强相似（余弦 ≥ 阈值）。

        只取「可能被误判」的候选（已 superseded 但还没过窗口、且语义上强命中当前 query），
        交给服务层做复活守卫（superseded_by 是否还活着）。
        """
        distance = CopilotMemory.embedding.cosine_distance(query_vec)
        max_distance = 1.0 - similarity_threshold
        rows = await self._session.scalars(
            select(CopilotMemory)
            .where(
                CopilotMemory.kind.in_(kinds),
                CopilotMemory.superseded.is_(True),
                CopilotMemory.superseded_at.is_not(None),
                CopilotMemory.superseded_at >= now - window,
                CopilotMemory.embedding.is_not(None),
                distance < max_distance,
            )
            .order_by(distance)
            .limit(top_k)
        )
        return list(rows)

    async def list_active(self, kind: MemoryKind) -> list[CopilotMemory]:
        """某 kind 的全部未 superseded 条目（constraint 硬召回 / 容量淘汰候选）。"""
        rows = await self._session.scalars(
            select(CopilotMemory).where(
                CopilotMemory.kind == kind, CopilotMemory.superseded.is_(False)
            )
        )
        return list(rows)

    async def get_by_entity(self, kind: MemoryKind, entity_id: str) -> CopilotMemory | None:
        """fact 同实体未 superseded 的最新条目（用于覆盖）。"""
        rows = await self._session.scalars(
            select(CopilotMemory)
            .where(
                CopilotMemory.kind == kind,
                CopilotMemory.entity_id == entity_id,
                CopilotMemory.superseded.is_(False),
            )
            .order_by(CopilotMemory.version.desc())
            .limit(1)
        )
        return rows.first()

    async def overwrite_entity(
        self,
        memory_id: uuid.UUID,
        content: str,
        embedding: list[float],
        trigger_conditions: dict[str, Any] | None,
    ) -> CopilotMemory:
        """fact 同实体覆盖：单条原子 UPDATE，version 由 SQL 侧自增。

        并发覆盖时「读 version → +1 → 写」会丢版本（两个 Agent 都从同一旧值 +1），
        这里用 ``version = version + 1`` 交给 DB 原子累加，杜绝丢 version。
        """
        await self._session.execute(
            update(CopilotMemory)
            .where(CopilotMemory.id == memory_id)
            .values(
                content=content,
                embedding=embedding,
                trigger_conditions=trigger_conditions,
                version=CopilotMemory.version + 1,
                updated_at=func.now(),
            )
        )
        await self._session.commit()
        memory = await self._session.get(CopilotMemory, memory_id)
        assert memory is not None
        await self._session.refresh(memory)
        return memory

    async def count_active(self, kind: MemoryKind) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(CopilotMemory)
            .where(CopilotMemory.kind == kind, CopilotMemory.superseded.is_(False))
        )
        return int(total or 0)

    async def touch(self, memory_ids: list[uuid.UUID], now: datetime) -> None:
        """命中回写：access_count+1、last_access=now（单条 Core UPDATE，不阻塞召回）。"""
        if not memory_ids:
            return
        await self._session.execute(
            update(CopilotMemory)
            .where(CopilotMemory.id.in_(memory_ids))
            .values(access_count=CopilotMemory.access_count + 1, last_access=now)
        )
        await self._session.commit()


class SqlAlchemyCopilotEventRepository:
    """事件日志 SQLAlchemy 实现（append-only，无更新）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_event(self, event: CopilotEvent) -> CopilotEvent:
        self._session.add(event)
        await self._session.commit()
        await self._session.refresh(event)
        return event

    async def list_events(self, run_id: uuid.UUID) -> list[CopilotEvent]:
        rows = await self._session.scalars(
            select(CopilotEvent)
            .where(CopilotEvent.run_id == run_id)
            .order_by(CopilotEvent.seq.asc(), CopilotEvent.id.asc())
        )
        return list(rows)
