"""Copilot 工具：create_note 幂等 / update_profile 文件 / 结果截断。"""

import asyncio
import uuid
from functools import partial
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import BaseTool

from app.agent.handoff import HandoffPacket
from app.agent.helpers import extract_llm_text
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.result import ToolFailure
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.rag_subagent import RagSubagent
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


def make_tools(
    tmp_path: Path,
) -> tuple[list[BaseTool], FakeNoteRepository, FakeKnowledgeBaseRepository, FakeDocumentRepository]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_repo = FakeNoteRepository()
    note_service = NoteService(note_repo, kb_repo)
    doc_repo = FakeDocumentRepository()
    document_service = DocumentService(doc_repo, kb_repo, FakeFileStore())
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
    breaker = CircuitBreaker()
    rag_subagent_factory = partial(
        RagSubagent,
        FakeMessagesListChatModel(responses=[]),
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(),
        verifier=FakeSideEffectVerifier(),
        breaker=breaker,
        security_breaker=SecurityBreaker(),
        gateway=gateway,
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
        lock=asyncio.Lock(),
        breaker=breaker,
        idempotency_store=InMemoryIdempotencyStore(),
        rag_subagent_factory=rag_subagent_factory,
    )
    return tools, note_repo, kb_repo, doc_repo


def _tool(tools: list[BaseTool], name: str) -> BaseTool:
    return next(t for t in tools if t.name == name)


async def test_create_note_idempotent(tmp_path: Path) -> None:
    tools, note_repo, _, _ = make_tools(tmp_path)
    create_note = _tool(tools, "create_note")

    first = await create_note.ainvoke({"title": "报告", "content": "正文内容"})
    second = await create_note.ainvoke({"title": "报告", "content": "正文内容"})

    # 同正文去重：两次创建落到同一条笔记
    assert "报告" in first["summary"]
    assert "报告" in second["summary"]
    assert first["id"] == second["id"]
    notes, total = await note_repo.list(limit=10, offset=0)
    assert total == 1


async def test_create_note_idempotency_key_conflict(tmp_path: Path) -> None:
    """同幂等键不同参数 → 拒绝执行，不静默返回旧结果（文章 §5.4）。"""
    tools, note_repo, _, _ = make_tools(tmp_path)
    create_note = _tool(tools, "create_note")

    first = await create_note.ainvoke({"title": "A", "content": "正文一", "idempotency_key": "k1"})
    assert "已创建" in first["summary"]
    with pytest.raises(ToolFailure):
        await create_note.ainvoke(
            {"title": "B", "content": "正文二（不同）", "idempotency_key": "k1"}
        )
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 1  # 仅第一次真正建了笔记


async def test_update_profile_writes_file(tmp_path: Path) -> None:
    tools, _, _, _ = make_tools(tmp_path)
    update_profile = _tool(tools, "update_profile")

    result = await update_profile.ainvoke({"kind": "soul", "content": "说话要简洁"})
    assert "soul" in result["summary"]
    assert result["kind"] == "soul"
    assert (tmp_path / "soul.md").read_text(encoding="utf-8") == "说话要简洁"


async def test_result_spill(tmp_path: Path) -> None:
    tools, note_repo, _, _ = make_tools(tmp_path)
    read_note = _tool(tools, "read_note")
    read_result = _tool(tools, "read_tool_result")

    from app.models.note import Note

    long_note = await note_repo.add(Note(title="长文", content_markdown="长" * 500))
    result = await read_note.ainvoke({"note_id": str(long_note.id)})
    assert isinstance(result, dict)
    summary = result["summary"]
    assert "结果已落盘" in summary
    assert "read_tool_result" in summary
    path = summary.split("read_tool_result(path='")[1].split("')")[0]
    # 全文读取有硬上限（资源契约）：超 max_result_chars 截断，防落盘全文灌爆上下文；
    # 提示用 grep_pattern 缩小范围，而非整段取回。
    full = await read_result.ainvoke({"path": path})
    assert "已截断" in full["summary"]
    assert "grep_pattern" in full["summary"]


def test_extract_llm_text() -> None:
    """表现层分离：dict 取 summary、list 逐元素、其他 str() 兜底。"""
    assert extract_llm_text("纯文本") == "纯文本"
    assert extract_llm_text({"summary": "给LLM读的", "items": [{"id": "1"}]}) == "给LLM读的"
    assert extract_llm_text({"no_summary": "x"}) == ""  # 无 summary → 空串
    assert extract_llm_text([1, 2]) == "1\n2"


def test_handoff_packet_to_task_prompt() -> None:
    """交棒包：task_description + constraints + available_tools + input_refs 拼成任务提示。"""
    packet = HandoffPacket(
        task_description="读文档",
        constraints=["红线：不泄露隐私"],
        available_tools=["read_document"],
        input_refs={"doc_id": "abc"},
    )
    result = packet.to_task_prompt()
    assert "读文档" in result
    assert "红线：不泄露隐私" in result
    assert "read_document" in result
    assert "doc_id: abc" in result
    assert "Input references" in result


def test_handoff_packet_empty() -> None:
    """交棒包：空字段只回 task_description。"""
    packet = HandoffPacket(task_description="读文档")
    assert packet.to_task_prompt() == "读文档\n"


async def test_list_documents_structured(tmp_path: Path) -> None:
    """list_documents 返回结构化 dict：summary 给 LLM、items 数组给程序（for 遍历）。"""
    from app.models.document import Document, DocumentType
    from app.models.knowledge_base import KnowledgeBase

    tools, _, kb_repo, doc_repo = make_tools(tmp_path)
    kb = await kb_repo.add(KnowledgeBase(name="工作"))
    await doc_repo.add(Document(kb_id=kb.id, title="文档A", source_type=DocumentType.MARKDOWN))
    await doc_repo.add(Document(kb_id=kb.id, title="文档B", source_type=DocumentType.URL))

    list_documents = _tool(tools, "list_documents")
    result = await list_documents.ainvoke({"kb_id": str(kb.id)})

    assert isinstance(result, dict)
    assert set(result) == {"summary", "items"}
    assert len(result["items"]) == 2
    assert {it["title"] for it in result["items"]} == {"文档A", "文档B"}
    assert all(set(it) == {"id", "title", "source_type"} for it in result["items"])
    assert "文档A" in result["summary"] and "文档B" in result["summary"]


async def test_spawn_plan_not_ready(tmp_path: Path) -> None:
    """spawn_plan 在 plan 子 Agent 未就绪（holder 空，测试环境）时返回友好文案，不报错。"""
    tools, _, _, _ = make_tools(tmp_path)
    spawn_plan = _tool(tools, "spawn_plan")
    result = await spawn_plan.ainvoke({"task": "总结所有文档"})
    assert "未就绪" in result["summary"]


async def test_spawn_reactive_exists(tmp_path: Path) -> None:
    """spawn_reactive 工具存在且能调用（测试环境 spawn_state=None 不计数、子 agent 未注入）。"""
    tools, _, _, _ = make_tools(tmp_path)
    spawn_reactive = _tool(tools, "spawn_reactive")
    assert spawn_reactive.name == "spawn_reactive"
