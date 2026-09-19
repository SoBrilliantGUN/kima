import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import api_router, health_router
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.core.exceptions import DomainError
from app.core.logging import RequestIdMiddleware, setup_logging
from app.core.storage import LocalFileStore
from app.integrations import get_document_parser, get_embedding_client
from app.integrations.web import start_browser, stop_browser
from app.repositories.document import SqlAlchemyDocumentRepository
from app.repositories.knowledge_base import SqlAlchemyKnowledgeBaseRepository
from app.repositories.note_chunk import SqlAlchemyNoteChunkRepository
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note_vectorize import NoteVectorizeService
from app.workers.document_worker import DocumentWorker
from app.workers.note_vectorize_worker import NoteVectorizeWorker


async def seed_default_knowledge_base() -> None:
    """应用启动时幂等预置默认知识库「我的知识库」。"""
    async with async_session_factory() as session:
        service = KnowledgeBaseService(SqlAlchemyKnowledgeBaseRepository(session))
        await service.ensure_default()


def build_document_worker() -> DocumentWorker:
    """用真实依赖构造文档处理 worker（每个操作独立 session）。"""
    settings = get_settings()
    parser = get_document_parser(settings)
    embedder = get_embedding_client(settings)
    file_store = LocalFileStore()

    return DocumentWorker(
        repo_factory=lambda: SqlAlchemyDocumentRepository(async_session_factory()),
        ingest_factory=lambda: IngestService(
            repository=SqlAlchemyDocumentRepository(async_session_factory()),
            parser=parser,
            embedder=embedder,
            file_store=file_store,
        ),
    )


def build_note_vectorize_worker() -> NoteVectorizeWorker:
    """用真实依赖构造笔记向量化 worker（每个操作独立 session）。"""
    settings = get_settings()
    embedder = get_embedding_client(settings)
    return NoteVectorizeWorker(
        repo_factory=lambda: SqlAlchemyNoteChunkRepository(async_session_factory()),
        service_factory=lambda: NoteVectorizeService(
            repository=SqlAlchemyNoteChunkRepository(async_session_factory()),
            embedder=embedder,
        ),
        idle_seconds=settings.note_revectorize_idle_seconds,
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await seed_default_knowledge_base()
    await start_browser()
    document_worker_task = asyncio.create_task(build_document_worker().run())
    note_worker_task = asyncio.create_task(build_note_vectorize_worker().run())
    yield
    for task in (document_worker_task, note_worker_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await stop_browser()


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": str(exc)}},
        )

    # 健康检查裸挂（探针）；业务路由挂 /api 前缀
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    return app


app = create_app()
