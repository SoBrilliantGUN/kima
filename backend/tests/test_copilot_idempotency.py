"""写工具幂等去重：幂等表状态机 + 业务意图键派生（对照《Agent Tools 的幂等性》§3/§6）。

覆盖幂等表核心状态机：原子抢占（inserted）、结果缓存命中（cached）、同键不同参数冲突
（conflict）、处理中（processing）、永久失败（failed_final）、processing 过期惰性回收；
以及业务意图键派生（内容派生、跨工具/跨 run 隔离）与 request_hash 参数指纹。
"""

import uuid
from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent.toolmeta import idempotency_key_for, request_hash_for
from app.agent.tools import build_tools
from app.core.memory_store import FileMemoryStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.models.note import Note
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.repositories.idempotency import (
    IdempotencyOutcome,
    InMemoryIdempotencyStore,
)
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeMemoryClassifier,
    FakeNoteRepository,
    make_gateway,
)


def test_idempotency_key_content_derived() -> None:
    """幂等键由业务身份字段派生：同字段同键、不同字段不同键、含 scope 与 tool_name。"""
    args = {"title": "a", "content": "正文", "kb_id": None}
    k1 = idempotency_key_for("create_note", args, ("content",), "r1")
    k2 = idempotency_key_for("create_note", args, ("content",), "r1")
    assert k1 == k2
    assert k1.startswith("r1:create_note:")
    # 不同 content → 不同键
    k3 = idempotency_key_for("create_note", {**args, "content": "别的内容"}, ("content",), "r1")
    assert k3 != k1
    # 不同 scope（run）→ 不同键（跨 run 隔离）
    k4 = idempotency_key_for("create_note", args, ("content",), "r2")
    assert k4 != k1
    # 不同工具 → 不同键
    k5 = idempotency_key_for("write_memory", args, ("content",), "r1")
    assert k5 != k1


def test_request_hash_detects_param_change() -> None:
    """request_hash 是完整参数指纹：任何参数变化都改变 hash（同键不同参数冲突检测的依据）。"""
    h1 = request_hash_for({"title": "a", "content": "正文", "kb_id": None})
    assert request_hash_for({"title": "a", "content": "正文", "kb_id": None}) == h1
    # title 变化（非 key_fields，但仍属完整参数）→ hash 不同
    assert request_hash_for({"title": "b", "content": "正文", "kb_id": None}) != h1


async def test_claim_succeed_then_cached() -> None:
    """同一请求重复调用：首次抢占执行、成功后命中缓存返回相同结果（§6 重复 N 次一致）。"""
    store = InMemoryIdempotencyStore()
    assert (await store.claim("create_note", "k1", "h1")).outcome is IdempotencyOutcome.INSERTED
    await store.succeed("create_note", "k1", "已创建笔记 N1")
    claim2 = await store.claim("create_note", "k1", "h1")
    assert claim2.outcome is IdempotencyOutcome.CACHED
    assert claim2.response == "已创建笔记 N1"


async def test_claim_conflict_same_key_different_hash() -> None:
    """同键不同参数 → 冲突（§5.4 拒绝，不静默返回旧结果）。"""
    store = InMemoryIdempotencyStore()
    assert (await store.claim("create_note", "k1", "h1")).outcome is IdempotencyOutcome.INSERTED
    assert (await store.claim("create_note", "k1", "h2")).outcome is IdempotencyOutcome.CONFLICT


async def test_claim_processing_then_expired_reclaim() -> None:
    """processing 未过期 → 处理中；过期（崩溃残留孤儿）→ 惰性回收重执行。"""
    store = InMemoryIdempotencyStore(ttl_seconds=86400)
    assert (await store.claim("create_note", "k1", "h1")).outcome is IdempotencyOutcome.INSERTED
    # 未 succeed（模拟崩溃在 claim 后 succeed 前），未过期 → processing
    assert (await store.claim("create_note", "k1", "h1")).outcome is IdempotencyOutcome.PROCESSING

    # ttl=0 的 store：processing 立即可被回收（expires_at 已过期）
    expired = InMemoryIdempotencyStore(ttl_seconds=0)
    assert (await expired.claim("create_note", "k2", "h1")).outcome is IdempotencyOutcome.INSERTED
    assert (await expired.claim("create_note", "k2", "h1")).outcome is IdempotencyOutcome.INSERTED


async def test_fail_then_failed_final() -> None:
    """永久失败 → failed_final，后续同键调用返回相同永久失败。"""
    store = InMemoryIdempotencyStore()
    assert (await store.claim("create_note", "k1", "h1")).outcome is IdempotencyOutcome.INSERTED
    await store.fail("create_note", "k1", "kb 不存在")
    claim = await store.claim("create_note", "k1", "h1")
    assert claim.outcome is IdempotencyOutcome.FAILED_FINAL
    assert claim.error == "kb 不存在"


class _EmptyRetrievalRepo:
    async def search_dense(
        self, kb_ids: list[uuid.UUID], query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def search_lexical(
        self, kb_ids: list[uuid.UUID], query: str, top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        return {}


class _CountingNoteService(NoteService):
    """计数版 NoteService：统计 create_with_content 被真正执行的次数（缓存命中不应计数）。"""

    def __init__(
        self, repository: FakeNoteRepository, kb_repository: FakeKnowledgeBaseRepository
    ) -> None:
        super().__init__(repository, kb_repository)
        self.create_calls = 0

    async def create_with_content(
        self, title: str, content_markdown: str, kb_id: uuid.UUID | None = None
    ) -> Note:
        self.create_calls += 1
        return await super().create_with_content(title, content_markdown, kb_id)


def _make_tools(
    tmp_path: Path, store: InMemoryIdempotencyStore
) -> tuple[list[BaseTool], FakeNoteRepository, _CountingNoteService]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_repo = FakeNoteRepository()
    note_service = _CountingNoteService(note_repo, kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    embedder = FakeEmbeddingClient(dimension=8)
    gateway = make_gateway(embedder=embedder, reranker=FakeRerankerClient())
    retriever = RagRetriever(
        repository=_EmptyRetrievalRepo(), gateway=gateway
    )
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=gateway,
        judge=FakeConflictJudge(),
        classifier=FakeMemoryClassifier(),
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )
    tools, _registry = build_tools(
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        max_result_chars=100,
        idempotency_store=store,
    )
    return tools, note_repo, note_service


async def test_create_note_same_key_reuses_cached_result(tmp_path: Path) -> None:
    """端到端 Effectively-once：同 key 同参数重复调用，第二次命中缓存、不重复执行写。"""
    tools, note_repo, note_service = _make_tools(tmp_path, InMemoryIdempotencyStore())
    create_note = next(t for t in tools if t.name == "create_note")

    first = await create_note.ainvoke({"title": "A", "content": "正文", "idempotency_key": "k1"})
    second = await create_note.ainvoke({"title": "A", "content": "正文", "idempotency_key": "k1"})
    assert first == second
    assert note_service.create_calls == 1  # 第二次缓存命中，未再次执行写
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 1
