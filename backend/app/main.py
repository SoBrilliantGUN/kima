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
from app.repositories.knowledge_base import SqlAlchemyKnowledgeBaseRepository
from app.services.knowledge_base import KnowledgeBaseService


async def seed_default_knowledge_base() -> None:
    """应用启动时幂等预置默认知识库「我的知识库」。"""
    async with async_session_factory() as session:
        service = KnowledgeBaseService(SqlAlchemyKnowledgeBaseRepository(session))
        await service.ensure_default()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await seed_default_knowledge_base()
    yield


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
