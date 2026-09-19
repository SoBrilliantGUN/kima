"""笔记向量化 worker：DB 轮询，拾取「停止编辑后到期」的已入库笔记重向量化。

与文档 worker 并列（同为单进程轮询）；`asyncio.Semaphore` 限制并发 embedding 调用。
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.models.note import Note
from app.repositories.note_chunk import NoteChunkRepository
from app.services.note_vectorize import NoteVectorizeService

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
DEFAULT_CONCURRENCY = 2
DEFAULT_POLL_INTERVAL = 10.0


class NoteVectorizeWorker:
    """按仓库/服务工厂驱动的轮询循环（工厂便于测试注入 Fake）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], NoteChunkRepository],
        service_factory: Callable[[], NoteVectorizeService],
        idle_seconds: int,
        limit: int = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._repo_factory = repo_factory
        self._service_factory = service_factory
        self._idle_seconds = idle_seconds
        self._limit = limit
        self._concurrency = concurrency
        self._poll_interval = poll_interval

    async def run_once(self) -> int:
        repository = self._repo_factory()
        try:
            due = await repository.claim_due(
                datetime.now(UTC), timedelta(seconds=self._idle_seconds), self._limit
            )
        finally:
            await repository.close()

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _process(note: Note) -> None:
            async with semaphore:
                service = self._service_factory()
                try:
                    await service.vectorize(note.id)
                finally:
                    await service.close()

        await asyncio.gather(*(_process(note) for note in due))
        return len(due)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("note vectorize worker 处理一轮失败")
            await asyncio.sleep(self._poll_interval)
