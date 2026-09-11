"""健康检查接口。

提供 Kubernetes/负载均衡常用的存活探针（liveness）与就绪探针（readiness）端点。
"""

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import __version__
from app.api.deps import DBSessionDep, SettingsDep
from app.schemas.health import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live(settings: SettingsDep) -> HealthResponse:
    """存活探针：仅确认服务进程在运行，不依赖数据库等外部组件。"""
    return HealthResponse(status="ok", database=None, version=__version__, app=settings.app_name)


@router.get("/ready", response_model=HealthResponse)
async def ready(response: Response, settings: SettingsDep, db: DBSessionDep) -> HealthResponse:
    """就绪探针：校验数据库连通性，失败时返回 503 并标记状态为 degraded。"""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return HealthResponse(
            status="degraded", database="error", version=__version__, app=settings.app_name
        )
    return HealthResponse(status="ok", database="ok", version=__version__, app=settings.app_name)
