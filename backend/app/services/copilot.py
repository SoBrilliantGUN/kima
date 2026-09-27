"""Copilot 记忆服务：三型召回 / 冲突判定 / 激活衰减 / 容量淘汰 / 软删除复活的编排。

策略集中在服务层，仓库只做存取原语（见 `repositories/copilot.py`）。召回逻辑在
``copilot_recall.py``（CopilotRecallMixin），写入路径在 ``copilot_write.py``
（CopilotWriteMixin），共享常量/类型/RRF 融合在 ``copilot_common.py``。
"""

import math
from datetime import datetime, timedelta

from app.agent.gateway import LLMGateway
from app.agent.memory_classifier import MemoryClassifier
from app.models.copilot import CopilotMemory, MemoryKind
from app.repositories.copilot import CopilotMemoryRepository
from app.services.conflict import ConflictJudge
from app.services.copilot_common import ACCESS_LOG_FACTOR, RECALL_FLOOR, RECENCY_BONUS
from app.services.copilot_common import RecalledMemories as RecalledMemories  # 对外重导出
from app.services.copilot_recall import CopilotRecallMixin
from app.services.copilot_write import CopilotWriteMixin


class CopilotMemoryService(CopilotRecallMixin, CopilotWriteMixin):
    """积累记忆（情节/事实/偏好）的召回与写入编排。"""

    def __init__(
        self,
        *,
        repository: CopilotMemoryRepository,
        gateway: LLMGateway,
        judge: ConflictJudge,
        episodic_ttl_days: int,
        recency_window_days: int,
        conflict_top_k: int,
        classifier: MemoryClassifier,
        superseded_window_days: int = 7,
        revival_similarity: float = 0.5,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._judge = judge
        self._episodic_ttl_days = episodic_ttl_days
        self._recency_window = timedelta(days=recency_window_days)
        self._conflict_top_k = conflict_top_k
        # 写入时分类器（防线一兜底：覆盖 Agent 自报 kind，恒在场）
        self._classifier = classifier
        # 软删除窗口（遗忘第三动作）：被 superseded 后 N 天内可召回复活 + 复活相似度阈值
        self._superseded_window = timedelta(days=superseded_window_days)
        self._revival_similarity = revival_similarity

    # --- 激活值（遗忘公式，见 module-6 §2.4） ---

    def compute_activation(self, memory: CopilotMemory, now: datetime) -> float:
        """activation ∈ [0, 1]，越高越活跃。

        非情节记忆（constraint/fact/preference）恒 1.0 上场——约束强制在场、事实由图谱
        覆写、偏好无 TTL 不走时间衰减，它们的遗忘只走冲突路径（superseded）。只有情节
        走完整三因子：base_decay（时间）+ frequency_gain（访问次数）+ recency_gain（近期），
        并 clamp 回 [0, 1]。
        """
        if memory.kind != MemoryKind.EPISODIC:
            return 1.0
        ttl_days = max(int(memory.ttl_days or self._episodic_ttl_days), 1)
        age_days = max((now - memory.created_at).days, 0)
        base = math.exp(-age_days / ttl_days)
        frequency = math.log(1 + max(0, memory.access_count or 0)) * ACCESS_LOG_FACTOR
        recency = 0.0
        if memory.last_access is not None and (now - memory.last_access).days < self._recency_window.days:
            recency = RECENCY_BONUS
        return min(1.0, base + frequency + recency)

    def _above_floor(self, memory: CopilotMemory, now: datetime) -> bool:
        return self.compute_activation(memory, now) >= RECALL_FLOOR

    async def _embed(self, node: str, text: str) -> list[float]:
        """内容向量化：只经网关（统一门禁/记账/快照）。调用方须已进入 ``run_budget``。"""
        return await self._gateway.embed_query(node, text)
