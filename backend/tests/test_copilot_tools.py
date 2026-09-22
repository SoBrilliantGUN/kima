"""Copilot 工具：create_note 幂等 / update_profile 文件 / 结果截断 / 非法入参。"""

import uuid
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from app.agent.resilience.result import ToolFailure
from app.agent.tools import build_tools
from app.core.memory_store import FileMemoryStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
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
    FakeNoteRepository,
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
    retriever = RagRetriever(
        repository=_EmptyRetrievalRepo(), embedder=embedder, reranker=FakeRerankerClient()
    )
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        embedder=embedder,
        judge=FakeConflictJudge(),
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


async def test_create_note_idempotency_key(tmp_path: Path) -> None:
    """位置幂等键命中 → 直接返回缓存，不重复建笔记（执行契约）。"""
    tools, note_repo = make_tools(tmp_path)
    create_note = _tool(tools, "create_note")

    first = await create_note.ainvoke(
        {"title": "A", "content": "正文一", "idempotency_key": "k1"}
    )
    second = await create_note.ainvoke(
        {"title": "B", "content": "正文二（不同）", "idempotency_key": "k1"}
    )
    assert first == second  # 幂等键命中，返回缓存结果
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 1


async def test_update_profile_writes_file(tmp_path: Path) -> None:
    tools, _ = make_tools(tmp_path)
    update_profile = _tool(tools, "update_profile")

    result = await update_profile.ainvoke({"kind": "soul", "content": "说话要简洁"})
    assert "soul" in result
    assert (tmp_path / "soul.md").read_text(encoding="utf-8") == "说话要简洁"


async def test_update_profile_invalid_kind(tmp_path: Path) -> None:
    tools, _ = make_tools(tmp_path)
    update_profile = _tool(tools, "update_profile")
    with pytest.raises(ToolFailure):
        await update_profile.ainvoke({"kind": "bogus", "content": "x"})


async def test_write_memory_invalid_kind(tmp_path: Path) -> None:
    tools, _ = make_tools(tmp_path)
    write_memory = _tool(tools, "write_memory")
    with pytest.raises(ToolFailure) as exc:
        await write_memory.ainvoke({"kind": "bogus", "content": "x"})
    assert "procedural" in str(exc.value)  # 错误提示里包含合法取值


async def test_read_note_invalid_uuid(tmp_path: Path) -> None:
    tools, _ = make_tools(tmp_path)
    read_note = _tool(tools, "read_note")
    with pytest.raises(ToolFailure) as exc:
        await read_note.ainvoke({"note_id": "not-a-uuid"})
    assert "不是合法 UUID" in str(exc.value)


async def test_result_spill(tmp_path: Path) -> None:
    tools, note_repo = make_tools(tmp_path)
    read_note = _tool(tools, "read_note")
    read_result = _tool(tools, "read_tool_result")

    from app.models.note import Note

    long_note = await note_repo.add(Note(title="长文", content_markdown="长" * 500))
    result = await read_note.ainvoke({"note_id": str(long_note.id)})
    assert "结果已落盘" in result
    assert "read_tool_result" in result
    # read_tool_result 能按占位符路径取回完整内容
    path = result.split("read_tool_result(path='")[1].split("')")[0]
    full = await read_result.ainvoke({"path": path})
    assert "长" * 500 in full
