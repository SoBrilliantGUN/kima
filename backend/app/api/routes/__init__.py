from fastapi import APIRouter

from app.api.routes import health, knowledge_bases, notes

# 业务 router（模块 2+ 填充），挂 /api 前缀
api_router = APIRouter()
api_router.include_router(knowledge_bases.router)
api_router.include_router(notes.router)

# 健康检查 router，裸挂（供探针），不加 /api 前缀
health_router = health.router

__all__ = ["api_router", "health_router"]
