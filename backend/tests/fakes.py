import math
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from app.agent.guardrail.review import ReviewResult, ReviewVerdict
from app.integrations.llm import ChatMessage, ChatResult
from app.integrations.parser import ParsedDocument, ParserError, SourceType
from app.models.copilot import CopilotEvent, CopilotMemory, MemoryKind
from app.models.document import MAX_RETRIES, Document, DocumentChunk, DocumentStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.note import Note
from app.services.copilot import ConflictVerdict


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

    async def get_by_content_hash(self, content_hash: str) -> Note | None:
        for note in self._store.values():
            if note.content_hash == content_hash:
                return note
        return None


class FakeDocumentRepository:
    """内存版文档 repository，模拟 SQLAlchemy 实现（分配 id/时间戳 + 父子 chunk 存储）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Document] = {}
        self._chunks: dict[uuid.UUID, list[DocumentChunk]] = {}

    async def add(self, doc: Document) -> Document:
        doc.id = uuid.uuid4()
        now = datetime.now(UTC)
        doc.created_at = now
        doc.updated_at = now
        if doc.status is None:
            doc.status = DocumentStatus.PENDING
        if doc.retry_count is None:
            doc.retry_count = 0
        self._store[doc.id] = doc
        return doc

    async def get(self, doc_id: uuid.UUID) -> Document | None:
        return self._store.get(doc_id)

    async def update(self, doc: Document) -> Document:
        doc.updated_at = datetime.now(UTC)
        self._store[doc.id] = doc
        return doc

    async def delete(self, doc: Document) -> None:
        self._store.pop(doc.id, None)
        self._chunks.pop(doc.id, None)

    async def claim_pending(self, limit: int) -> list[Document]:
        now = datetime.now(UTC)
        pending = [
            d
            for d in self._store.values()
            if d.status == DocumentStatus.PENDING
            and (d.next_retry_at is None or d.next_retry_at <= now)
        ]
        pending.sort(key=lambda d: d.created_at)
        claimed = pending[:limit]
        for doc in claimed:
            doc.status = DocumentStatus.PROCESSING
            doc.error_message = None
        return claimed

    async def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        for chunk in chunks:
            self._chunks.setdefault(chunk.document_id, []).append(chunk)

    async def delete_chunks(self, doc_id: uuid.UUID) -> None:
        self._chunks.pop(doc_id, None)

    async def recover_stuck(self, now: datetime, max_age: timedelta) -> int:
        cutoff = now - max_age
        stuck = [
            d
            for d in self._store.values()
            if d.status == DocumentStatus.PROCESSING and d.updated_at <= cutoff
        ]
        for doc in stuck:
            doc.retry_count += 1
            if doc.retry_count > MAX_RETRIES:
                doc.status = DocumentStatus.ERROR
                doc.next_retry_at = None
            else:
                doc.status = DocumentStatus.PENDING
                doc.next_retry_at = now
            doc.error_message = "处理超时，已重置"
        return len(stuck)

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Document]:
        docs = [d for d in self._store.values() if d.kb_id == kb_id]
        return sorted(docs, key=lambda d: (d.updated_at, d.id), reverse=True)

    async def close(self) -> None:
        """内存版无连接池，close 为空操作（对齐 SQLAlchemy 实现的会话关闭）。"""

    # 测试辅助：直接读取/注入，避开 async 语义
    def chunks_of(self, doc_id: uuid.UUID) -> list[DocumentChunk]:
        return self._chunks.get(doc_id, [])


class FakeFileStore:
    """内存文件存储，记录保存/删除供断言。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def save(self, content: bytes, ext: str) -> str:
        name = f"{uuid.uuid4().hex}.{ext}"
        self.files[name] = content
        return name

    async def open(self, path: str) -> bytes:
        return self.files[path]

    async def delete(self, path: str) -> None:
        if path in self.files:
            self.files.pop(path)
            self.deleted.append(path)


class FakeDocumentParser:
    """假分发器（满足 DocumentParser 胖签名），ingest/worker 注入用。"""

    def __init__(
        self,
        markdown: str = "# 解析正文",
        title: str = "解析标题",
        *,
        fail: bool = False,
    ) -> None:
        self.markdown = markdown
        self.title = title
        self.fail = fail
        self.calls: list[str] = []

    async def parse(
        self,
        *,
        source_type: SourceType,
        content: bytes | None = None,
        url: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        self.calls.append(source_type.value)
        if self.fail:
            raise ParserError("解析失败")
        return ParsedDocument(markdown=self.markdown, title=self.title, metadata={})


class FakeFileParser:
    """窄文件解析器替身（同时满足 PdfParser / WordParser），记录调用并返回固定 markdown。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def parse(self, *, content: bytes, filename: str | None = None) -> ParsedDocument:
        self.calls.append(self.name)
        return ParsedDocument(markdown=f"# {self.name}")


class FakeUrlParser:
    """窄 URL 解析器替身（满足 UrlParser），记录调用并返回固定 markdown。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def parse(self, *, url: str) -> ParsedDocument:
        self.calls.append(self.name)
        return ParsedDocument(markdown=f"# {self.name}")


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """两向量余弦距离（1 - 余弦相似度），FakeCopilotMemoryRepository.search 用。"""
    if not a or not b:
        return 1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


class FakeCopilotMemoryRepository:
    """内存版记忆仓库：分配 id/时间戳，search 按余弦距离排序（对齐 pgvector 语义）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, CopilotMemory] = {}

    async def add(self, memory: CopilotMemory) -> CopilotMemory:
        memory.id = uuid.uuid4()
        now = datetime.now(UTC)
        if memory.created_at is None:
            memory.created_at = now
        if memory.updated_at is None:
            memory.updated_at = now
        self._store[memory.id] = memory
        return memory

    async def get(self, memory_id: uuid.UUID) -> CopilotMemory | None:
        return self._store.get(memory_id)

    async def update(self, memory: CopilotMemory) -> CopilotMemory:
        memory.updated_at = datetime.now(UTC)
        self._store[memory.id] = memory
        return memory

    async def delete(self, memory: CopilotMemory) -> None:
        self._store.pop(memory.id, None)

    async def search(
        self, kind: MemoryKind, query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]:
        candidates = [
            m
            for m in self._store.values()
            if m.kind == kind and not m.superseded and m.embedding is not None
        ]
        candidates.sort(key=lambda m: _cosine_distance(m.embedding or [], query_vec))
        return candidates[:top_k]

    async def list_active(self, kind: MemoryKind) -> list[CopilotMemory]:
        return [m for m in self._store.values() if m.kind == kind and not m.superseded]

    async def get_by_entity(self, kind: MemoryKind, entity_id: str) -> CopilotMemory | None:
        matches = [
            m
            for m in self._store.values()
            if m.kind == kind and m.entity_id == entity_id and not m.superseded
        ]
        return max(matches, key=lambda m: m.version) if matches else None

    async def count_active(self, kind: MemoryKind) -> int:
        return len([m for m in self._store.values() if m.kind == kind and not m.superseded])

    async def touch(self, memory_ids: list[uuid.UUID], now: datetime) -> None:
        for memory_id in memory_ids:
            memory = self._store.get(memory_id)
            if memory is not None:
                memory.access_count += 1
                memory.last_access = now


class FakeCopilotEventRepository:
    """内存版事件日志仓库：自增 seq，记录调用供断言。"""

    def __init__(self) -> None:
        self.events: list[CopilotEvent] = []
        self._seq = 0

    async def add_event(self, event: CopilotEvent) -> CopilotEvent:
        self._seq += 1
        event.seq = self._seq
        event.id = uuid.uuid4()
        event.created_at = datetime.now(UTC)
        self.events.append(event)
        return event

    async def list_events(self, run_id: uuid.UUID) -> list[CopilotEvent]:
        return [e for e in self.events if e.run_id == run_id]


class FakeConflictJudge:
    """确定性冲突判定：按给定 verdicts 逐条回放（不足补 none），记录调用。"""

    def __init__(self, verdicts: list[str] | None = None) -> None:
        self._verdicts = verdicts or []
        self.calls: list[tuple[str, list[str]]] = []

    async def judge(self, new_content: str, candidates: list[str]) -> list[ConflictVerdict]:
        self.calls.append((new_content, candidates))
        verdicts = list(self._verdicts)
        verdicts = verdicts + ["none"] * (len(candidates) - len(verdicts))
        return [ConflictVerdict(v) for v in verdicts[: len(candidates)]]


class FakeOutputReviewer:
    """脚本化输出审查器：按给定 results 逐次回放（不足回退 ok），记录调用。"""

    def __init__(self, results: list[ReviewResult] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[str, str]] = []

    async def review(self, final_answer: str, trace: str) -> ReviewResult:
        self.calls.append((final_answer, trace))
        if self._results:
            return self._results.pop(0)
        return ReviewResult(verdict=ReviewVerdict.OK, issues=[])


class ScriptedLLM:
    """按序回放内容的自定义 LLMClient（测试自纠错循环：先错后对）。

    ``prompt_tokens`` / ``completion_tokens`` 可选，逐次对齐 ``contents``，用于测试
    预算记账（不足时回退 0）。
    """

    def __init__(
        self,
        contents: list[str],
        prompt_tokens: list[int] | None = None,
        completion_tokens: list[int] | None = None,
    ) -> None:
        self._contents = list(contents)
        self._prompt_tokens = list(prompt_tokens or [])
        self._completion_tokens = list(completion_tokens or [])
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.calls.append(messages)
        content = self._contents.pop(0) if self._contents else ""
        prompt = self._prompt_tokens.pop(0) if self._prompt_tokens else 0
        completion = self._completion_tokens.pop(0) if self._completion_tokens else 0
        return ChatResult(content=content, prompt_tokens=prompt, completion_tokens=completion)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self._contents:
            yield self._contents.pop(0)

