"""Copilot 工具：create_note 幂等 / update_profile 文件 / 结果截断。"""

import asyncio
import uuid
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import BaseTool

from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.result import ToolFailure
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.tools import build_tools
from app.core.memory_store import FileMemoryStore
from app.core.skill_store import FileSkillStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.repositories.idempotency import InMemoryIdempotencyStore
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
    FakeOutputReviewer,
    FakeSideEffectVerifier,
    make_gateway,
    make_runtime_config,
)


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


def make_tools(tmp_path: Path) -> tuple[list[BaseTool], FakeNoteRepository]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_repo = FakeNoteRepository()
    note_service = NoteService(note_repo, kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    embedder = FakeEmbeddingClient(dimension=8)
    gateway = make_gateway(embedder=embedder, reranker=FakeRerankerClient())
    retriever = RagRetriever(repository=_EmptyRetrievalRepo(), gateway=gateway)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=gateway,
        judge=FakeConflictJudge(),
        classifier=FakeMemoryClassifier(),
        episodic_ttl_days=30,
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
        skill_store=FileSkillStore(tmp_path / "skills"),
        max_result_chars=100,
        model=FakeMessagesListChatModel(responses=[]),
        gateway=gateway,
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(),
        verifier=FakeSideEffectVerifier(),
        security_breaker=SecurityBreaker(),
        lock=asyncio.Lock(),
        breaker=CircuitBreaker(),
        idempotency_store=InMemoryIdempotencyStore(),
    )
    return tools, note_repo


def _tool(tools: list[BaseTool], name: str) -> BaseTool:
    return next(t for t in tools if t.name == name)


async def test_create_note_idempotent(tmp_path: Path) -> None:
    tools, note_repo = make_tools(tmp_path)
    create_note = _tool(tools, "create_note")

    first = await create_note.ainvoke({"title": "报告", "content": "正文内容"})
    second = await create_note.ainvoke({"title": "报告", "content": "正文内容"})

    # 同正文去重：两次创建落到同一条笔记
    assert "报告" in first
    assert "报告" in second
    notes, total = await note_repo.list(limit=10, offset=0)
    assert total == 1


async def test_create_note_idempotency_key_conflict(tmp_path: Path) -> None:
    """同幂等键不同参数 → 拒绝执行，不静默返回旧结果（文章 §5.4）。"""
    tools, note_repo = make_tools(tmp_path)
    create_note = _tool(tools, "create_note")

    first = await create_note.ainvoke({"title": "A", "content": "正文一", "idempotency_key": "k1"})
    assert "已创建" in first
    with pytest.raises(ToolFailure):
        await create_note.ainvoke(
            {"title": "B", "content": "正文二（不同）", "idempotency_key": "k1"}
        )
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 1  # 仅第一次真正建了笔记


async def test_update_profile_writes_file(tmp_path: Path) -> None:
    tools, _ = make_tools(tmp_path)
    update_profile = _tool(tools, "update_profile")

    result = await update_profile.ainvoke({"kind": "soul", "content": "说话要简洁"})
    assert "soul" in result
    assert (tmp_path / "soul.md").read_text(encoding="utf-8") == "说话要简洁"


async def test_result_spill(tmp_path: Path) -> None:
    tools, note_repo = make_tools(tmp_path)
    read_note = _tool(tools, "read_note")
    read_result = _tool(tools, "read_tool_result")

    from app.models.note import Note

    long_note = await note_repo.add(Note(title="长文", content_markdown="长" * 500))
    result = await read_note.ainvoke({"note_id": str(long_note.id)})
    assert "结果已落盘" in result
    assert "read_tool_result" in result
    path = result.split("read_tool_result(path='")[1].split("')")[0]
    # 全文读取有硬上限（资源契约）：超 max_result_chars 截断，防落盘全文灌爆上下文；
    # 提示用 grep_pattern 缩小范围，而非整段取回。
    full = await read_result.ainvoke({"path": path})
    assert "已截断" in full
    assert "grep_pattern" in full
