import uuid
from datetime import UTC, datetime

from app.models.knowledge_base import KnowledgeBase


class FakeKnowledgeBaseRepository:
    """内存版 repository，模拟 SQLAlchemy 实现的插入语义（分配 id + 时间戳）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, KnowledgeBase] = {}

    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]:
        items = sorted(
            self._store.values(),
            key=lambda kb: (kb.updated_at, kb.id),
            reverse=True,
        )
        return items[offset : offset + limit], len(items)

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self._store.get(kb_id)

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        for kb in self._store.values():
            if kb.name == name:
                return kb
        return None

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        kb.id = uuid.uuid4()
        now = datetime.now(UTC)
        kb.created_at = now
        kb.updated_at = now
        self._store[kb.id] = kb
        return kb

    async def update(self, kb: KnowledgeBase) -> KnowledgeBase:
        kb.updated_at = datetime.now(UTC)
        self._store[kb.id] = kb
        return kb

    async def delete(self, kb: KnowledgeBase) -> None:
        self._store.pop(kb.id, None)
