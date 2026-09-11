from collections.abc import AsyncIterator
from typing import Any

from httpx import AsyncClient

from app.core.db import get_db_session
from app.main import app


class _FakeSession:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    async def execute(self, statement: Any) -> Any:
        if not self._healthy:
            raise RuntimeError("database unavailable")
        return None


async def _override_healthy() -> AsyncIterator[_FakeSession]:
    yield _FakeSession(healthy=True)


async def _override_broken() -> AsyncIterator[_FakeSession]:
    yield _FakeSession(healthy=False)


async def test_health_live(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_ready_ok(client: AsyncClient) -> None:
    app.dependency_overrides[get_db_session] = _override_healthy
    try:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["database"] == "ok"
    finally:
        app.dependency_overrides.clear()


async def test_health_ready_degraded(client: AsyncClient) -> None:
    app.dependency_overrides[get_db_session] = _override_broken
    try:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "error"
    finally:
        app.dependency_overrides.clear()
