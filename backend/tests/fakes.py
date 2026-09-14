import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.models.knowledge_base import KnowledgeBase
from app.models.note import Note


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


class FakeNoteRepository:
    """内存版笔记 repository，模拟 SQLAlchemy 实现（分配 id + 时间戳 + 关联幂等）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Note] = {}
        self._associations: set[tuple[uuid.UUID, uuid.UUID]] = set()

    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]:
        items = sorted(
            self._store.values(),
            key=lambda note: (note.updated_at, note.id),
            reverse=True,
        )
        return items[offset : offset + limit], len(items)

    async def get(self, note_id: uuid.UUID) -> Note | None:
        return self._store.get(note_id)

    async def add(self, note: Note) -> Note:
        note.id = uuid.uuid4()
        now = datetime.now(UTC)
        note.created_at = now
        note.updated_at = now
        self._store[note.id] = note
        return note

    async def update(self, note: Note) -> Note:
        note.updated_at = datetime.now(UTC)
        self._store[note.id] = note
        return note

    async def delete(self, note: Note) -> None:
        self._store.pop(note.id, None)
        self._associations = {
            (n, k) for (n, k) in self._associations if n != note.id
        }

    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool:
        key = (note_id, kb_id)
        if key in self._associations:
            return False
        self._associations.add(key)
        return True

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Note]:
        note_ids = {n for (n, k) in self._associations if k == kb_id}
        notes = [self._store[n] for n in note_ids if n in self._store]
        return sorted(notes, key=lambda note: (note.updated_at, note.id), reverse=True)
