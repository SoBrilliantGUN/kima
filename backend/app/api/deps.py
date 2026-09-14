"""FastAPI 依赖注入模块。

集中定义各层对象的创建逻辑（settings、数据库会话、外部客户端、
仓库、服务），并通过 Annotated 类型别名暴露给路由层使用。
路由里只需声明形如 `repo: KnowledgeBaseRepositoryDep` 即可完成注入，
无需关心具体实现如何构造。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.integrations import get_document_parser, get_embedding_client, get_llm_client
from app.integrations.embedding import EmbeddingClient
from app.integrations.llm import LLMClient
from app.integrations.parser import DocumentParser
from app.integrations.web import (
    FallbackWebFetcher,
    PlaywrightWebFetcher,
    TrafilaturaWebFetcher,
    WebFetcher,
    get_browser,
)
from app.repositories.knowledge_base import (
    KnowledgeBaseRepository,
    SqlAlchemyKnowledgeBaseRepository,
)
from app.repositories.note import NoteRepository, SqlAlchemyNoteRepository
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm import Summarizer
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
    """按 settings.embedding_provider 返回对应的 Embedding 客户端（当前仅 fake）。"""
    return get_embedding_client(settings)


def get_document_parser_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> DocumentParser:
    """返回文档解析器（模块 1 为 fake，模块 4 接入 MinerU 后替换实现）。"""
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


def get_web_fetcher_dep() -> WebFetcher:
    """返回网页抓取器：先静态 trafilatura，正文为空时回退到 Playwright 渲染 SPA。"""
    browser = get_browser()
    spa = PlaywrightWebFetcher(browser) if browser is not None else None
    return FallbackWebFetcher(TrafilaturaWebFetcher(), spa)


def get_note_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NoteRepository:
    """用当前数据库会话构造笔记仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyNoteRepository(session)


def get_summarizer_dep(
    llm: Annotated[LLMClient, Depends(get_llm_client_dep)],
) -> Summarizer:
    """用 LLM 客户端构造摘要生成器（LLM 相关操作统一经此注入）。"""
    return Summarizer(llm)


def get_note_service(
    repo: Annotated[NoteRepository, Depends(get_note_repository)],
    kb_repo: Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)],
    web_fetcher: Annotated[WebFetcher, Depends(get_web_fetcher_dep)],
    summarizer: Annotated[Summarizer, Depends(get_summarizer_dep)],
) -> NoteService:
    """构造笔记服务，注入仓库、知识库仓库、抓取器与摘要生成器。"""
    return NoteService(repo, kb_repo, web_fetcher, summarizer)


# Annotated 类型别名：把「类型 + 依赖函数」打包，路由签名直接使用这些名字完成注入。
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
DocumentParserDep = Annotated[DocumentParser, Depends(get_document_parser_dep)]
WebFetcherDep = Annotated[WebFetcher, Depends(get_web_fetcher_dep)]
KnowledgeBaseRepositoryDep = Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)]
KnowledgeBaseServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]
NoteRepositoryDep = Annotated[NoteRepository, Depends(get_note_repository)]
NoteServiceDep = Annotated[NoteService, Depends(get_note_service)]

__all__ = [
    "SettingsDep",
    "DBSessionDep",
    "LLMClientDep",
    "EmbeddingClientDep",
    "DocumentParserDep",
    "WebFetcherDep",
    "KnowledgeBaseRepositoryDep",
    "KnowledgeBaseServiceDep",
    "NoteRepositoryDep",
    "NoteServiceDep",
]
