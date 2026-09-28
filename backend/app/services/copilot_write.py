"""Copilot 记忆写入 mixin（冲突判定 / 容量淘汰 / 软删除退场）。

以 mixin 挂到 `CopilotMemoryService`：属性由 `CopilotMemoryService.__init__` 注入
（在类体声明类型，供严格 mypy 下 mixin 方法访问）。共享常量见 ``copilot_common.py``。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.agent.memory_classifier import MemoryClassifier
from app.models.copilot import CopilotMemory, MemoryKind
from app.repositories.copilot import CopilotMemoryRepository
from app.services.conflict import ConflictJudge, ConflictVerdict
from app.services.copilot_common import PREF_EPISODIC, SOFT_KINDS


class CopilotWriteMixin:
    # 由 CopilotMemoryService.__init__ 注入（此处声明类型，供 mixin 方法在严格 mypy 下访问）。
    _classifier: MemoryClassifier
    _repository: CopilotMemoryRepository
    _judge: ConflictJudge
    _conflict_top_k: int
    _episodic_ttl_days: int

    # 由 CopilotMemoryService 实现（mixin 依赖其存在，声明签名以通过类型检查）。
    async def _embed(self, node: str, text: str) -> list[float]:
        raise NotImplementedError

    async def write_memory(
        self, kind: MemoryKind, content: str, entity_id: str | None = None
    ) -> CopilotMemory:
        """写一条积累记忆（非追加，冲突判定 + 容量淘汰）。

        入口先跑分类器做防线一兜底：分类器的权威 kind / entity_id /
        trigger_conditions **覆盖** Agent 自报值——防止「禁止 ORM」被 Agent 当事实写入、
        从而走向量 top-k 漏召回。fact 同 `entity_id` 直接覆盖（version++）并清理
        矛盾的旧偏好/情节；其余走跨型 LLM 冲突判定（constraint 孤岛、软记忆互为参照）：
        DUPLICATE（同型语义等价）→ 去重不写、touch 旧记忆续命；CONTRADICTION → 写新 +
        旧 `superseded` 留痕（`superseded_at`/`superseded_by` 记软删除窗口）。
        """
        trigger_conditions: dict[str, Any] | None = None
        classification = await self._classifier.classify(content)
        if classification is not None:
            kind = classification.kind
            entity_id = entity_id or classification.entity_id
            trigger_conditions = classification.trigger_conditions

        if kind == MemoryKind.FACT and entity_id:
            existing = await self._repository.get_by_entity(kind, entity_id)
            if existing is not None:
                embedding = await self._embed("memory.write", content)
                # 原子覆盖：version 由 SQL 侧 ``version + 1`` 自增（并发不丢版本），
                # 不再走「读-改-写」的 `update(existing)`。
                memory = await self._repository.overwrite_entity(
                    existing.id, content, embedding, trigger_conditions
                )
                # 事实覆写后也做跨型整合：让矛盾的旧偏好/情节退场（不动其它事实）
                await self._consolidate_conflicts(
                    content, embedding, list(PREF_EPISODIC), memory.id
                )
                return memory

        embedding = await self._embed("memory.write", content)
        # 冲突候选范围：constraint 是红线孤岛（只与 constraint 冲突）；软记忆跨型互为参照
        candidate_kinds: list[MemoryKind] = (
            [MemoryKind.CONSTRAINT] if kind == MemoryKind.CONSTRAINT else list(SOFT_KINDS)
        )
        # 冲突预筛 + 裁决提到落盘之前：DUPLICATE 走「去重不写」，只有 CONTRADICTION/NONE 才写新
        candidates = await self._repository.search_cross_kind(
            candidate_kinds, embedding, self._conflict_top_k
        )
        verdicts = (
            await self._judge.judge(content, [c.content for c in candidates]) if candidates else []
        )
        now = datetime.now(UTC)
        # 去重分支（机制二「旧胜新丢」）：新记忆与某条同型已有记忆语义等价 → 不落盘，
        # 只 touch 旧记忆续命（检索强化），返回旧条目作为「这条内容」的承载者。
        for candidate, verdict in zip(candidates, verdicts, strict=False):
            if verdict is ConflictVerdict.DUPLICATE and candidate.kind is kind:
                await self._repository.touch([candidate.id], now)
                return candidate

        memory = CopilotMemory(
            kind=kind,
            content=content,
            entity_id=entity_id,
            embedding=embedding,
            ttl_days=self._episodic_ttl_days if kind == MemoryKind.EPISODIC else None,
            trigger_conditions=trigger_conditions,
            access_count=0,
            superseded=False,
            version=1,
        )
        memory = await self._repository.add(memory)
        # 新记忆落盘后再让矛盾的旧记忆退场（superseded_by 指向新记忆 id，供复活守卫用）
        await self._supersede_losers(candidates, verdicts, memory.id, now, include_duplicate=False)
        return memory

    async def _supersede_losers(
        self,
        candidates: list[CopilotMemory],
        verdicts: list[ConflictVerdict],
        winner_id: uuid.UUID,
        now: datetime,
        *,
        include_duplicate: bool,
    ) -> None:
        """把败者候选标记 superseded 留痕（软删除窗口起点 + 压它的赢家 id）。

        `include_duplicate` 控制「语义等价（DUPLICATE）」是否也算败者：事实覆写路径
        （旧偏好/情节与新高阶事实冗余也算冗余）传 True；普通写路径已在落盘前把同型
        DUPLICATE 走「去重不写」返回，此处只处理 CONTRADICTION 传 False。
        """
        skip = {winner_id}
        for candidate, verdict in zip(candidates, verdicts, strict=False):
            if candidate.id in skip:
                continue
            is_loser = verdict is ConflictVerdict.CONTRADICTION or (
                include_duplicate and verdict is ConflictVerdict.DUPLICATE
            )
            if is_loser:
                candidate.superseded = True
                candidate.superseded_at = now
                candidate.superseded_by = winner_id
                await self._repository.update(candidate)

    async def _consolidate_conflicts(
        self,
        content: str,
        embedding: list[float],
        candidate_kinds: list[MemoryKind],
        winner_id: uuid.UUID,
    ) -> None:
        """事实覆写路径的增量整合（机制二）：检索 Top-K 相似候选 → LLM 裁决 → 输家退场。

        冲突是局部的，用检索找候选、不做全库扫描；跨型也照此（偏好↔情节）。赢家 id 记到
        输家的 `superseded_by`，供召回时的复活守卫判断「压它的那条还活着吗」。

        与普通写路径不同：这里赢家是「被原地覆写的事实」本身（无新行），故 DUPLICATE
        也算输家（旧偏好/情节与新高阶事实冗余，一并退场）。
        """
        candidates = await self._repository.search_cross_kind(
            candidate_kinds, embedding, self._conflict_top_k
        )
        skip = {winner_id}
        candidates = [c for c in candidates if c.id not in skip]
        if not candidates:
            return
        verdicts = await self._judge.judge(content, [c.content for c in candidates])
        await self._supersede_losers(
            candidates,
            verdicts,
            winner_id,
            datetime.now(UTC),
            include_duplicate=True,
        )
