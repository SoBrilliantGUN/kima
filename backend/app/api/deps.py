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
from app.repositories.knowledge_base import (
    KnowledgeBaseRepository,
    SqlAlchemyKnowledgeBaseRepository,
)
from app.services.knowledge_base import KnowledgeBaseService


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


# Annotated 类型别名：把「类型 + 依赖函数」打包，路由签名直接使用这些名字完成注入。
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
DocumentParserDep = Annotated[DocumentParser, Depends(get_document_parser_dep)]
KnowledgeBaseRepositoryDep = Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)]
KnowledgeBaseServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]

__all__ = [
    "SettingsDep",
    "DBSessionDep",
    "LLMClientDep",
    "EmbeddingClientDep",
    "DocumentParserDep",
    "KnowledgeBaseRepositoryDep",
    "KnowledgeBaseServiceDep",
]
