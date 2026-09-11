from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.integrations import get_document_parser, get_embedding_client, get_llm_client
from app.integrations.embedding import EmbeddingClient
from app.integrations.llm import LLMClient
from app.integrations.parser import DocumentParser


def get_settings_dep() -> Settings:
    return get_settings()


def get_llm_client_dep(settings: Annotated[Settings, Depends(get_settings_dep)]) -> LLMClient:
    return get_llm_client(settings)


def get_embedding_client_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> EmbeddingClient:
    return get_embedding_client(settings)


def get_document_parser_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> DocumentParser:
    return get_document_parser(settings)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
DocumentParserDep = Annotated[DocumentParser, Depends(get_document_parser_dep)]

__all__ = [
    "SettingsDep",
    "DBSessionDep",
    "LLMClientDep",
    "EmbeddingClientDep",
    "DocumentParserDep",
]
