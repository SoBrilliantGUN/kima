"""笔记向量化 worker / service 单测：idle 阈值传递、删除+新增、vectorized_at 回写（注入 Fake）。"""

import uuid
from datetime import datetime, timedelta

from app.integrations.embedding import FakeEmbeddingClient
from app.models.note import Note
from app.models.note_chunk import NoteChunk
from app.services.note_vectorize import NoteVectorizeService
from app.workers.note_vectorize_worker import NoteVectorizeWorker


class FakeNoteChunkRepository:
    """内存版笔记分块仓库，记录删除/新增/回写调用。"""

    def __init__(self, notes: list[Note], due: list[Note] | None = None) -> None:
        self._notes = {note.id: note for note in notes}
        self._due = due or []
        self.deleted: list[uuid.UUID] = []
        self.added: list[NoteChunk] = []
        self.marked: list[tuple[uuid.UUID, datetime]] = []
        self.claim_args: tuple[datetime, timedelta, int] | None = None

    async def claim_due(self, now: datetime, idle: timedelta, limit: int) -> list[Note]:
        self.claim_args = (now, idle, limit)
        return list(self._due[:limit])

    async def get_note(self, note_id: uuid.UUID) -> Note | None:
        return self._notes.get(note_id)

    async def delete_chunks(self, note_id: uuid.UUID) -> None:
        self.deleted.append(note_id)

    async def add_chunks(self, chunks: list[NoteChunk]) -> None:
        self.added.extend(chunks)

    async def mark_vectorized(self, note_id: uuid.UUID, timestamp: datetime) -> None:
        self.marked.append((note_id, timestamp))

    async def close(self) -> None:
        return None


def _note(content: str) -> Note:
    return Note(id=uuid.uuid4(), title="t", content_markdown=content)


async def test_vectorize_deletes_and_adds() -> None:
    note = _note("# 标题\n\n一段内容，足够长以产生至少一个 chunk。")
    repo = FakeNoteChunkRepository([note])
    service = NoteVectorizeService(repository=repo, embedder=FakeEmbeddingClient(dimension=4))

    await service.vectorize(note.id)

    assert repo.deleted == [note.id]
    parents = [c for c in repo.added if c.parent_id is None]
    children = [c for c in repo.added if c.parent_id is not None]
    assert len(parents) >= 1
    assert len(children) >= 1
    # parent 不向量化，child 向量化
    assert all(c.embedding is None for c in parents)
    assert all(c.embedding is not None and len(c.embedding) == 4 for c in children)
    # child 的 parent_id 指向某个 parent
    parent_ids = {p.id for p in parents}
    assert all(c.parent_id in parent_ids for c in children)
    assert repo.marked and repo.marked[0][0] == note.id


async def test_vectorize_empty_note_marks_vectorized() -> None:
    note = _note("")
    repo = FakeNoteChunkRepository([note])
    service = NoteVectorizeService(repository=repo, embedder=FakeEmbeddingClient(dimension=4))

    await service.vectorize(note.id)

    assert repo.added == []
    assert repo.marked  # 空笔记也回写，避免下轮反复拾取


async def test_worker_passes_idle_threshold_and_processes() -> None:
    note = _note("内容")
    repo = FakeNoteChunkRepository([note], due=[note])
    worker = NoteVectorizeWorker(
        repo_factory=lambda: repo,
        service_factory=lambda: NoteVectorizeService(
            repository=repo, embedder=FakeEmbeddingClient(dimension=4)
        ),
        idle_seconds=120,
    )

    count = await worker.run_once()

    assert count == 1
    assert repo.claim_args is not None and repo.claim_args[1] == timedelta(seconds=120)
    assert repo.marked


async def test_worker_no_due_notes() -> None:
    repo = FakeNoteChunkRepository([])
    worker = NoteVectorizeWorker(
        repo_factory=lambda: repo,
        service_factory=lambda: NoteVectorizeService(
            repository=repo, embedder=FakeEmbeddingClient(dimension=4)
        ),
        idle_seconds=120,
    )

    count = await worker.run_once()

    assert count == 0
    assert repo.marked == []
