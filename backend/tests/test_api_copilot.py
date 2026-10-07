"""Copilot API 集成：SSE 冒烟 + 记忆/技能只读端点 + 会话 kind 过滤（注入 Fake）。"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.compose import CopilotRuntime, build_runtime
from app.api.deps_copilot import (
    get_copilot_memory_service,
    get_copilot_runtime,
    get_copilot_stream_event_repository,
    get_memory_file_store,
)
from app.api.deps_core import get_chat_service
from app.core.memory_store import FileMemoryStore
from app.core.skill_store import FileSkillStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.main import app
from app.models.chat import ChatConversation, ChatMessage
from app.models.copilot import CopilotStreamEvent
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.services.chat import ChatService
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotEventRepository,
    FakeCopilotMemoryRepository,
    FakeCopilotStreamEventRepository,
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeMemoryClassifier,
    FakeNoteRepository,
    FakeOutputReviewer,
    make_copilot_defaults,
    make_gateway,
    make_runtime_config,
)


class ScriptedAgentModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> "ScriptedAgentModel":
        return self


class _StubRunManager:
    """HTTP 冒烟用桩：不真正跑后台任务，只记录 start / submit_decision 调用。"""

    def __init__(self) -> None:
        self.started: list = []

    def start(self, prepared: object, request: object) -> None:
        self.started.append(prepared)

    def get(self, assistant_message_id: uuid.UUID) -> object | None:
        return None

    def submit_decision(self, assistant_message_id: uuid.UUID, decisions: list) -> bool:
        return True

    def cancel(self, assistant_message_id: uuid.UUID) -> bool:
        return True


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


class FakeChatRepository:
    def __init__(self) -> None:
        self._conversations: dict[uuid.UUID, ChatConversation] = {}
        self._messages: dict[uuid.UUID, list[ChatMessage]] = {}
        self.last_kind: str | None = None

    async def add_conversation(self, conversation: ChatConversation) -> ChatConversation:
        conversation.id = uuid.uuid4()
        now = datetime.now(UTC)
        conversation.created_at = now
        conversation.updated_at = now
        self._conversations[conversation.id] = conversation
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> ChatConversation | None:
        return self._conversations.get(conversation_id)

    async def list_conversations(
        self, kb_id: uuid.UUID | None, kind: str | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        self.last_kind = kind
        items = [c for c in self._conversations.values() if kind is None or c.kind == kind]
        return items[offset : offset + limit], len(items)

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        self._conversations.pop(conversation.id, None)

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        message.id = message.id or uuid.uuid4()
        message.created_at = datetime.now(UTC)
        self._messages.setdefault(message.conversation_id, []).append(message)
        return message

    async def update_message(self, message_id, *, content, steps):
        for msgs in getattr(self, "_messages", {}).values():
            for m in msgs:
                if m.id == message_id:
                    m.content = content
                    m.steps = steps
                    return m
        return ChatMessage(id=message_id, content=content, steps=steps)

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return list(self._messages.get(conversation_id, []))


def _make_copilot_service(
    tmp_path: Path,
) -> tuple[CopilotRuntime, FakeChatRepository, FileMemoryStore, CopilotMemoryService]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
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
    chat_repo = FakeChatRepository()
    memory_store = FileMemoryStore(tmp_path)
    rt = build_runtime(
        model=ScriptedAgentModel(responses=[AIMessage(content="这是回答")]),
        gateway=gateway,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=memory_store,
        skill_store=FileSkillStore(tmp_path / "skills"),
        chat_repository=chat_repo,
        event_repository=FakeCopilotEventRepository(),
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(),
        **make_copilot_defaults(note_service, memory_service),
    )
    return rt, chat_repo, memory_store, memory_service


@pytest.fixture
async def api_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    rt, chat_repo, memory_store, memory_service = _make_copilot_service(tmp_path)
    app.dependency_overrides[get_copilot_runtime] = lambda: rt
    app.dependency_overrides[get_memory_file_store] = lambda: memory_store
    app.dependency_overrides[get_copilot_memory_service] = lambda: memory_service
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        chat_repo,
        FakeKnowledgeBaseRepository(),
        None,  # type: ignore[arg-type]
    )
    run_manager = _StubRunManager()
    app.state.copilot_run_manager = run_manager
    app.state.stub_run_manager = run_manager  # 供断言 start 调用
    stream_repo = FakeCopilotStreamEventRepository()
    app.dependency_overrides[get_copilot_stream_event_repository] = lambda: stream_repo
    app.state.fake_stream_repo = stream_repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_copilot_chat_returns_ids(api_client: AsyncClient) -> None:
    """POST /chat 解耦后：同步准备 + 起后台任务，立即返回四个 id（非 SSE）。"""
    response = await api_client.post("/api/copilot/chat", json={"question": "帮我看看"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"conversation_id", "user_message_id", "assistant_message_id", "run_id"}
    # 后台任务已登记（桩记录 start 调用）
    assert len(app.state.stub_run_manager.started) == 1


async def test_copilot_run_stream_replays(api_client: AsyncClient) -> None:
    """GET /runs/{id}/stream 回放已持久化事件（无活 run → 回放完即关）。"""
    assistant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    stream_repo = app.state.fake_stream_repo
    # 预置事件：meta → delta → done
    meta_payload = {
        "conversation_id": str(uuid.uuid4()),
        "user_message_id": str(uuid.uuid4()),
        "assistant_message_id": str(assistant_id),
    }
    for type_, payload in [
        ("meta", meta_payload),
        ("delta", {"text": "你好"}),
        ("done", {"assistant_message_id": str(assistant_id)}),
    ]:
        event = CopilotStreamEvent(
            run_id=run_id, assistant_message_id=assistant_id, type=type_, payload=payload
        )
        await stream_repo.add_event(event)

    names: list[str] = []
    async with api_client.stream("GET", f"/api/copilot/runs/{assistant_id}/stream") as response:
        assert response.status_code == 200
        current_event: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: ") :]
            elif line.startswith("data: "):
                names.append(current_event or "")

    assert names == ["meta", "delta", "done"]


async def test_copilot_run_cancel(api_client: AsyncClient) -> None:
    """POST /runs/{id}/cancel 触发后台 run 取消（桩返回 ok）。"""
    response = await api_client.post(f"/api/copilot/runs/{uuid.uuid4()}/cancel")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_copilot_memory_endpoint(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/copilot/memory")
    assert response.status_code == 200
    body = response.json()
    assert body["soul"] == ""
    assert body["user"] == ""
    assert body["memories"] == []


async def test_copilot_skills_endpoint(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/copilot/skills")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 20
    write_names = {item["name"] for item in items if item["has_side_effect"]}
    assert write_names == {
        "create_knowledge_base",
        "create_note",
        "write_memory",
        "update_profile",
        "write_skill",
        "delete_skill",
    }


async def test_conversations_kind_filter(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/conversations", params={"kind": "copilot"})
    assert response.status_code == 200
