"""planner 执行图：supervisor-worker 并行执行 + replan 端到端。"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.compose import CopilotRuntime, build_runtime
from app.agent.orchestrate import make_config, make_tracker
from app.agent.runtime.plan_graph import build_plan_graph
from app.agent.runtime.planner import LLMPlanner
from app.core.memory_store import FileMemoryStore
from app.core.skill_store import FileSkillStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.models.chat import ChatMessage
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotEventRepository,
    FakeCopilotMemoryRepository,
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeMemoryClassifier,
    FakeNoteRepository,
    FakeOutputReviewer,
    ScriptedLLM,
    make_copilot_defaults,
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


class _FakeChatRepository:
    async def add_message(self, message: ChatMessage) -> ChatMessage:
        message.id = message.id or uuid.uuid4()
        message.created_at = datetime.now(UTC)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return []


def make_plan_runtime(tmp_path: Path, plan_json: str, answer: str) -> CopilotRuntime:
    """构造带脚本化 planner（LLMPlanner + ScriptedLLM 生成固定 plan）的 runtime。"""
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    gateway = make_gateway(
        embedder=FakeEmbeddingClient(dimension=8),
        reranker=FakeRerankerClient(),
        llm=ScriptedLLM([plan_json]),
    )
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
    defaults = make_copilot_defaults(note_service, memory_service)
    defaults["planner"] = LLMPlanner(gateway)
    return build_runtime(
        model=FakeMessagesListChatModel(responses=[AIMessage(content=answer)]),
        gateway=gateway,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        skill_store=FileSkillStore(tmp_path / "skills"),
        chat_repository=_FakeChatRepository(),
        event_repository=FakeCopilotEventRepository(),
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(),
        **defaults,
    )


def _initial_state(task: str) -> dict:
    return {
        "task": task,
        "system_prompt": "",
        "memory_block": "",
        "plan": {},
        "results": {},
        "failures": {},
        "trace": [],
        "completed_steps": [],
        "final_answer": "",
        "plan_error": "",
        "step_id": "",
        "last_error": None,
    }


async def test_plan_graph_parallel_steps(tmp_path: Path) -> None:
    """无依赖步骤应被 Send 扇出并行执行，results 收集全部产物。"""
    plan = {
        "steps": [
            {"id": "1", "action": "list_notes", "params": {}},
            {"id": "2", "action": "list_notes", "params": {}},
            {"id": "3", "action": "list_notes", "params": {}},
        ]
    }
    rt = make_plan_runtime(tmp_path, json.dumps(plan), "三份笔记都列好了。")
    graph = build_plan_graph(rt, make_tracker(rt))
    run_id = uuid.uuid4()
    config = make_config(rt, run_id, uuid.uuid4(), "列出所有笔记")
    final = await graph.ainvoke(_initial_state("列出所有笔记"), config=config)
    assert set(final["results"].keys()) == {"1", "2", "3"}
    assert final["final_answer"] == "三份笔记都列好了。"


async def test_plan_graph_replans_on_failure(tmp_path: Path) -> None:
    """失败步骤触发 replan；replan 拿不出替换步骤时 plan_error 落定、合成输出失败。"""
    plan = {"steps": [{"id": "1", "action": "read_note", "params": {"id": "missing"}}]}
    rt = make_plan_runtime(tmp_path, json.dumps(plan), "读不到。")
    graph = build_plan_graph(rt, make_tracker(rt))
    run_id = uuid.uuid4()
    config = make_config(rt, run_id, uuid.uuid4(), "读一篇笔记")
    final = await graph.ainvoke(_initial_state("读一篇笔记"), config=config)
    # read_note 读不到 → 失败 → replan 返回空（ScriptedLLM 用尽）→ plan_error 落定
    assert final["plan_error"]
    assert "失败" in final["final_answer"]
