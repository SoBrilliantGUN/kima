"""Copilot 记忆服务的共享常量 / 类型 / RRF 融合（供 recall / write 两个 mixin 与基类复用）。"""

import uuid
from dataclasses import dataclass

from app.models.copilot import CopilotMemory, MemoryKind

# 事实/偏好/情节召回 top-k（constraint 全量注入，不设上限）
MEMORY_RECALL_TOP_K = 5
# 词法召回 top-k（与 dense 各取一路，RRF 融合）
MEMORY_LEXICAL_TOP_K = 5
# RRF 融合的排名衰减常数（与 app/rag/hybrid.py 的 RRF_K 一致）
_RRF_K = 60

# activation 公式参数（见 module-6 §2.4）
RECALL_FLOOR = 0.05
ACCESS_LOG_FACTOR = 0.2
RECENCY_BONUS = 0.3

# 软记忆（事实/偏好/情节）——冲突候选跨型互为参照（约束是红线孤岛，不在此列）
SOFT_KINDS = (MemoryKind.FACT, MemoryKind.PREFERENCE, MemoryKind.EPISODIC)
# 事实覆写（fact 同 entity upsert）时，只清理矛盾的偏好/情节，不动其它事实
PREF_EPISODIC = (MemoryKind.PREFERENCE, MemoryKind.EPISODIC)


def fuse_memories(
    dense: list[CopilotMemory], lexical: list[CopilotMemory], k: int = _RRF_K
) -> list[CopilotMemory]:
    """dense + lexical 两路记忆 RRF 融合（按 memory.id 去重、按倒数排名降序）。

    语义/情节召回的多路混合：向量通道补语义、词法通道补精确串（表名/错误码），
    谁在某路排得更靠前谁赢；与 `app/rag/hybrid.rrf_fuse` 同算法，只是键从 chunk 换成
    memory.id（记忆条目没有 source_type 维度）。
    """
    scores: dict[uuid.UUID, float] = {}
    by_id: dict[uuid.UUID, CopilotMemory] = {}
    for results in (dense, lexical):
        for rank, memory in enumerate(results, start=1):
            scores[memory.id] = scores.get(memory.id, 0.0) + 1.0 / (k + rank)
            by_id.setdefault(memory.id, memory)
    return [by_id[mid] for mid, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)]


@dataclass
class RecalledMemories:
    """召回结果：四型分组，供 `format_memory_block` 格式化成 L2 注入块。

    constraint 是硬召回（全量、不过相似度阈值）；fact / preference / episodic 是
    混合召回（向量 + 词法 RRF + 软遗忘过滤）。
    """

    constraint: list[CopilotMemory]
    preference: list[CopilotMemory]
    fact: list[CopilotMemory]
    episodic: list[CopilotMemory]
