"""Copilot API 集成：SSE 冒烟 + 记忆/技能只读端点 + 会话 kind 过滤（注入 Fake）。"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.service import CopilotService
from app.api.deps import get_chat_service, get_copilot_service
from app.core.memory_store import FileMemoryStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.main import app
from app.models.chat import ChatConversation, ChatMessage
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
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeNoteRepository,
)


class ScriptedAgentModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> "ScriptedAgentModel":
        return self


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
        items = [
            c for c in self._conversations.values() if kind is None or c.kind == kind
        ]
        return items[offset : offset + limit], len(items)

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        self._conversations.pop(conversation.id, None)

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        message.id = message.id or uuid.uuid4()
        message.created_at = datetime.now(UTC)
        self._messages.setdefault(message.conversation_id, []).append(message)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return list(self._messages.get(conversation_id, []))


def _make_copilot_service(tmp_path: Path) -> tuple[CopilotService, FakeChatRepository]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
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
    chat_repo = FakeChatRepository()
    service = CopilotService(
        model=ScriptedAgentModel(responses=[AIMessage(content="这是回答")]),
        checkpointer=None,
        tracer=None,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        chat_repository=chat_repo,
        event_repository=FakeCopilotEventRepository(),
        max_result_chars=4000,
    )
    return service, chat_repo


@pytest.fixture
async def api_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    service, chat_repo = _make_copilot_service(tmp_path)
    app.dependency_overrides[get_copilot_service] = lambda: service
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        chat_repo, FakeKnowledgeBaseRepository(), None  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_copilot_chat_sse(api_client: AsyncClient) -> None:
    events: list[tuple[str | None, object]] = []
    async with api_client.stream(
        "POST", "/api/copilot/chat", json={"question": "帮我看看"}
    ) as response:
        assert response.status_code == 200
        current_event: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((current_event, json.loads(line[len("data: ") :])))

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert "delta" in names
    assert names[-1] == "done"


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
    assert len(items) == 11
    write_names = {item["name"] for item in items if item["has_side_effect"]}
    assert write_names == {"create_note", "write_memory", "update_profile"}


async def test_conversations_kind_filter(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/conversations", params={"kind": "copilot"})
    assert response.status_code == 200
