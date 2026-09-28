"""Copilot 记忆召回 mixin（三型召回 / 软删除复活 / 激活过滤 / 检索强化）。

以 mixin 挂到 `CopilotMemoryService`：属性由 `CopilotMemoryService.__init__` 注入
（在类体声明类型，供严格 mypy 下 mixin 方法访问）。共享常量/类型/RRF 融合见
``copilot_common.py``。
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.models.copilot import CopilotMemory, MemoryKind
from app.repositories.copilot import CopilotMemoryRepository
from app.services.copilot_common import (
    MEMORY_LEXICAL_TOP_K,
    MEMORY_RECALL_TOP_K,
    SOFT_KINDS,
    RecalledMemories,
    fuse_memories,
)


class CopilotRecallMixin:
    # 由 CopilotMemoryService.__init__ 注入（此处声明类型，供 mixin 方法在严格 mypy 下访问）。
    _repository: CopilotMemoryRepository
    _revival_similarity: float
    _superseded_window: timedelta

    # 由 CopilotMemoryService 实现（mixin 依赖其存在，声明签名以通过类型检查）。
    def _above_floor(self, memory: CopilotMemory, now: datetime) -> bool:
        raise NotImplementedError

    async def _embed(self, node: str, text: str) -> list[float]:
        raise NotImplementedError

    async def recall(self, query: str) -> RecalledMemories:
        """注入用分路召回（四阶段并行、按优先级串行合并）。

        - constraint：硬召回全量（list_active，不过相似度阈值）——约束是确定域，
          「只要任务沾边就必须在场」，不靠余弦相似度碰运气。
        - fact / preference / episodic：混合召回 = dense 向量 + lexical 词法（BM25）RRF
          融合 + 软遗忘过滤。
        - 先做软删除复活（机制二）：窗口期内被强命中的 superseded 软记忆翻回 active，
          再走正常召回路径；最后对命中条目回写 access_count/last_access（检索强化）。
        """
        now = datetime.now(UTC)
        query_vec = await self._embed("memory.recall", query)
        # 1. 软删除复活：误判的 superseded 记忆若强命中当前 query，翻回 active
        await self._revive_recoverable(query_vec, now)
        constraint = await self._repository.list_active(MemoryKind.CONSTRAINT)
        preference_hits = await self._hybrid_recall(MemoryKind.PREFERENCE, query, query_vec)
        fact_hits = await self._hybrid_recall(MemoryKind.FACT, query, query_vec)
        episodic_hits = await self._hybrid_recall(MemoryKind.EPISODIC, query, query_vec)
        preference = [m for m in preference_hits if self._above_floor(m, now)]
        fact = [m for m in fact_hits if self._above_floor(m, now)]
        episodic = [m for m in episodic_hits if self._above_floor(m, now)]
        # 2. 检索强化：命中回写 access_count+1 / last_access（ACT-R 频率/近期增益的活水）
        hit_ids = [m.id for m in preference + fact + episodic]
        if hit_ids:
            await self._repository.touch(hit_ids, now)
        return RecalledMemories(
            constraint=constraint,
            preference=preference,
            fact=fact,
            episodic=episodic,
        )

    async def _revive_recoverable(self, query_vec: list[float], now: datetime) -> None:
        """软删除窗口复活：窗口期内强命中的 superseded 软记忆翻回 active（superseded=False）。

        两道门槛：
        - 复活守卫：压它的那条记忆（superseded_by）还活着，说明真冲突仍成立，不复活——
          防「真矛盾」被召回误复活、新旧状态再次平权共存。
        - 激活门槛：衰减到 recall floor 以下的不复活（否则复活后照样被 floor 拦下，
          徒留一条永远不上场的僵尸 active 行）。
        """
        recoverable = await self._repository.search_recoverable(
            list(SOFT_KINDS),
            query_vec,
            self._revival_similarity,
            now,
            self._superseded_window,
            MEMORY_RECALL_TOP_K,
        )
        if not recoverable:
            return
        # 复活守卫批量查：一次取出所有「压它的赢家」，判断还活着吗（避免逐条 get 的 N+1）
        superseder_ids = [m.superseded_by for m in recoverable if m.superseded_by is not None]
        alive = {m.id for m in await self._repository.get_many(superseder_ids) if not m.superseded}
        revived: list[uuid.UUID] = []
        for memory in recoverable:
            if memory.superseded_by in alive:
                continue  # 压它的那条还活着 → 不复活
            if not self._above_floor(memory, now):
                continue  # 已衰减到地板以下 → 复活了也上不了场，别制造僵尸
            memory.superseded = False
            memory.superseded_at = None
            memory.superseded_by = None
            await self._repository.update(memory)
            revived.append(memory.id)
        if revived:
            # 复活即「又被想起来」：回写频率/近期，给激活值续命
            await self._repository.touch(revived, now)

    async def _hybrid_recall(
        self, kind: MemoryKind, query: str, query_vec: list[float]
    ) -> list[CopilotMemory]:
        """单一 kind 的混合召回：dense + lexical RRF 融合后取 top-k。"""
        dense = await self._repository.search(kind, query_vec, MEMORY_RECALL_TOP_K)
        lexical = await self._repository.search_lexical(kind, query, MEMORY_LEXICAL_TOP_K)
        return fuse_memories(dense, lexical)[:MEMORY_RECALL_TOP_K]

    async def list_all(self) -> list[CopilotMemory]:
        """三型全部未 superseded 条目（只读记忆面板用）。"""
        result: list[CopilotMemory] = []
        for kind in MemoryKind:
            result.extend(await self._repository.list_active(kind))
        return result

    async def get_memory(self, memory_id: uuid.UUID) -> CopilotMemory | None:
        """按 id 取一条记忆（写工具副作用确定性对账用）。"""
        return await self._repository.get(memory_id)

    async def search_memory(
        self, query: str, kind: MemoryKind | None, top_k: int = MEMORY_RECALL_TOP_K
    ) -> list[CopilotMemory]:
        """按需读记忆（工具）：语义检索命中并回写 access_count/last_access（批回写）。"""
        now = datetime.now(UTC)
        query_vec = await self._embed("memory.search", query)
        kinds = [kind] if kind is not None else [MemoryKind.FACT, MemoryKind.EPISODIC]
        hits: list[CopilotMemory] = []
        for k in kinds:
            hits.extend(await self._repository.search(k, query_vec, top_k))
        hits = [m for m in hits if self._above_floor(m, now)]
        if hits:
            await self._repository.touch([m.id for m in hits], now)
        return hits
