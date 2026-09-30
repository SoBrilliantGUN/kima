"""FastAPI 依赖注入：模块 1–5（settings / DB 会话 / 外部客户端 / 仓库 / 服务）。

模块 6 的 Copilot 依赖见 ``deps_copilot.py``；路由层的 ``*Dep`` 别名与 ``__all__`` 在
``deps.py`` 集中定义，本模块只放各层对象的创建函数。
"""

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.gateway import LLMGateway
from app.agent.runtime.budget import DailyBudget
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.storage import FileStore, LocalFileStore
from app.integrations import (
    get_document_parser,
    get_embedding_client,
    get_llm_client,
    get_reranker_client,
    get_web_search_client,
)
from app.integrations.embedding import EmbeddingClient
from app.integrations.llm import LLMClient
from app.integrations.parser import DocumentParser
from app.integrations.rerank import RerankerClient
from app.integrations.search import WebSearchClient
from app.rag.repository import SqlAlchemyRetrievalRepository
from app.rag.retriever import RagRetriever
from app.rag.service import RagService
from app.repositories.chat import ChatRepository, SqlAlchemyChatRepository
from app.repositories.document import DocumentRepository, SqlAlchemyDocumentRepository
from app.repositories.knowledge_base import (
    KnowledgeBaseRepository,
    SqlAlchemyKnowledgeBaseRepository,
)
from app.repositories.note import NoteRepository, SqlAlchemyNoteRepository
from app.services.chat import ChatService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService


def get_settings_dep() -> Settings:
    """提供全局缓存的应用配置（由 get_settings 的 lru_cache 保证单例）。"""
    return get_settings()


def get_llm_client_dep(settings: Annotated[Settings, Depends(get_settings_dep)]) -> LLMClient:
    """按 settings.llm_provider 返回对应的 LLM 客户端（当前仅 fake）。"""
    return get_llm_client(settings)


def get_embedding_client_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> EmbeddingClient:
    """按 settings.embedding_provider 返回对应的 Embedding 客户端。"""
    return get_embedding_client(settings)


def get_document_parser_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> DocumentParser:
    """返回文档解析器（按 source_type 分发的 DispatchDocumentParser）。"""
    return get_document_parser(settings)


def get_kb_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowledgeBaseRepository:
    """用当前数据库会话构造知识库仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyKnowledgeBaseRepository(session)


def get_kb_service(
    repo: Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)],
) -> KnowledgeBaseService:
    """构造知识库服务，注入仓库实现。"""
    return KnowledgeBaseService(repo)


def get_note_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NoteRepository:
    """用当前数据库会话构造笔记仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyNoteRepository(session)


def get_note_service(
    repo: Annotated[NoteRepository, Depends(get_note_repository)],
    kb_repo: Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)],
) -> NoteService:
    """构造笔记服务，注入笔记仓库与知识库仓库。"""
    return NoteService(repo, kb_repo)


def get_file_store() -> FileStore:
    """返回本地磁盘文件存储（模块 4 文档原文件落盘/读取）。"""
    return LocalFileStore()


def get_document_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentRepository:
    """用当前数据库会话构造文档仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyDocumentRepository(session)


def get_document_service(
    repo: Annotated[DocumentRepository, Depends(get_document_repository)],
    kb_repo: Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)],
    file_store: Annotated[FileStore, Depends(get_file_store)],
) -> DocumentService:
    """构造文档服务，注入文档仓库、知识库仓库与文件存储。"""
    return DocumentService(repo, kb_repo, file_store)


def get_reranker_client_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> RerankerClient:
    """按 settings.rerank_provider 返回对应的 rerank 客户端。"""
    return get_reranker_client(settings)


def get_web_search_client_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> WebSearchClient:
    """按 settings.web_search_provider 返回对应的全网搜索客户端。"""
    return get_web_search_client(settings)


def get_chat_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ChatRepository:
    """用当前数据库会话构造会话仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyChatRepository(session)


def get_llm_gateway(request: Request) -> LLMGateway:
    """LLM 网关（app 级单例，lifespan 装配，见 main.py）。模块 5/6 共用——所有 LLM 调用
    唯一出口，没有裸 embedder/reranker/LLMClient 的说法。"""
    gateway = getattr(request.app.state, "copilot_gateway", None)
    if gateway is None:
        raise RuntimeError("LLM 网关未在 lifespan 装配（app.state.copilot_gateway）")
    return cast(LLMGateway, gateway)


def get_daily_budget(request: Request) -> DailyBudget | None:
    """跨 run 日预算（app 级单例；未装配为 None = 关闭日预算）。"""
    return cast(DailyBudget | None, getattr(request.app.state, "copilot_daily_budget", None))


def get_rag_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    web_search: Annotated[WebSearchClient, Depends(get_web_search_client_dep)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    daily_budget: Annotated[DailyBudget | None, Depends(get_daily_budget)],
) -> RagService:
    """构造 RAG 服务（检索编排 + 生成），检索层绑定当前数据库会话，LLM/embed/rerank 全经网关。"""
    retriever = RagRetriever(
        repository=SqlAlchemyRetrievalRepository(session),
        gateway=gateway,
        rerank_min_score=settings.rerank_min_score,
    )
    return RagService(
        retriever=retriever,
        gateway=gateway,
        web_search=web_search,
        context_max_tokens=settings.context_max_tokens,
        history_recent_turns=settings.history_recent_turns,
        daily_budget=daily_budget,
    )


def get_chat_service(
    repo: Annotated[ChatRepository, Depends(get_chat_repository)],
    kb_repo: Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)],
    rag: Annotated[RagService, Depends(get_rag_service)],
) -> ChatService:
    """构造会话服务，注入会话仓库、知识库仓库与 RAG 服务。"""
    return ChatService(repo, kb_repo, rag)
