"""FastAPI 依赖注入对外入口。

各层对象的创建函数分属 ``deps_core``（模块 1–5）与 ``deps_copilot``（模块 6）；
本模块集中定义 Annotated 类型别名（``*Dep``），路由里只需声明形如
`repo: KnowledgeBaseRepositoryDep` 即可完成注入，无需关心具体实现如何构造。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.compose import CopilotRuntime
from app.api.deps_copilot import (
    get_copilot_memory_service,
    get_copilot_runtime,
    get_copilot_stream_event_repository,
    get_memory_file_store,
    get_skill_file_store,
)
from app.api.deps_core import (
    get_chat_repository,
    get_chat_service,
    get_document_parser_dep,
    get_document_repository,
    get_document_service,
    get_embedding_client_dep,
    get_kb_repository,
    get_kb_service,
    get_llm_client_dep,
    get_note_repository,
    get_note_service,
    get_rag_service,
    get_reranker_client_dep,
    get_settings_dep,
    get_web_search_client_dep,
)
from app.core.config import Settings
from app.core.db import get_db_session
from app.core.memory_store import MemoryFileStore
from app.core.skill_store import SkillFileStore
from app.integrations.embedding import EmbeddingClient
from app.integrations.llm import LLMClient
from app.integrations.parser import DocumentParser
from app.integrations.rerank import RerankerClient
from app.integrations.search import WebSearchClient
from app.rag.service import RagService
from app.repositories.chat import ChatRepository
from app.repositories.copilot import CopilotStreamEventRepository
from app.repositories.document import DocumentRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.note import NoteRepository
from app.services.chat import ChatService
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService

# Annotated 类型别名：把「类型 + 依赖函数」打包，路由签名直接使用这些名字完成注入。
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
DocumentParserDep = Annotated[DocumentParser, Depends(get_document_parser_dep)]
KnowledgeBaseRepositoryDep = Annotated[KnowledgeBaseRepository, Depends(get_kb_repository)]
KnowledgeBaseServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]
NoteRepositoryDep = Annotated[NoteRepository, Depends(get_note_repository)]
NoteServiceDep = Annotated[NoteService, Depends(get_note_service)]
DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
RerankerClientDep = Annotated[RerankerClient, Depends(get_reranker_client_dep)]
WebSearchClientDep = Annotated[WebSearchClient, Depends(get_web_search_client_dep)]
ChatRepositoryDep = Annotated[ChatRepository, Depends(get_chat_repository)]
RagServiceDep = Annotated[RagService, Depends(get_rag_service)]
ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
CopilotRuntimeDep = Annotated[CopilotRuntime, Depends(get_copilot_runtime)]
CopilotStreamEventRepositoryDep = Annotated[
    CopilotStreamEventRepository, Depends(get_copilot_stream_event_repository)
]
MemoryFileStoreDep = Annotated[MemoryFileStore, Depends(get_memory_file_store)]
CopilotMemoryServiceDep = Annotated[CopilotMemoryService, Depends(get_copilot_memory_service)]
SkillFileStoreDep = Annotated[SkillFileStore, Depends(get_skill_file_store)]

__all__ = [
    "SettingsDep",
    "DBSessionDep",
    "LLMClientDep",
    "EmbeddingClientDep",
    "DocumentParserDep",
    "KnowledgeBaseRepositoryDep",
    "KnowledgeBaseServiceDep",
    "NoteRepositoryDep",
    "NoteServiceDep",
    "DocumentRepositoryDep",
    "DocumentServiceDep",
    "RerankerClientDep",
    "WebSearchClientDep",
    "ChatRepositoryDep",
    "RagServiceDep",
    "ChatServiceDep",
    "CopilotRuntimeDep",
    "CopilotStreamEventRepositoryDep",
    "MemoryFileStoreDep",
    "CopilotMemoryServiceDep",
    "SkillFileStoreDep",
]
