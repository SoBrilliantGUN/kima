"""笔记向量化 worker：事件驱动，拾取「停止编辑后到期」的已入库笔记重向量化。

与文档 worker 并列（同为事件驱动）；`asyncio.Semaphore` 限制并发 embedding 调用。
无新/更新笔记时 idle（`wake_event.wait`），不空转；超时兜底按 idle 周期再查一遍。
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.wake_events import note_wake_event
from app.models.note import Note
from app.repositories.note_chunk import NoteChunkRepository
from app.services.note_vectorize import NoteVectorizeService

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
DEFAULT_CONCURRENCY = 2


class NoteVectorizeWorker:
    """按仓库/服务工厂驱动的事件驱动循环（工厂便于测试注入 Fake）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], NoteChunkRepository],
        service_factory: Callable[[], NoteVectorizeService],
        idle_seconds: int,
        limit: int = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY,
        idle_fallback_seconds: float | None = None,
        wake_event: asyncio.Event | None = None,
    ) -> None:
        self._repo_factory = repo_factory
        self._service_factory = service_factory
        self._idle_seconds = idle_seconds
        self._limit = limit
        self._concurrency = concurrency
        # 兜底周期默认 = 笔记 idle 阈值（编辑后约 idle_seconds 再查一次，保持向量化时效）
        self._idle_fallback_seconds = idle_fallback_seconds or float(idle_seconds)
        self._wake_event = wake_event or note_wake_event

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
            # 有唤醒立即处理；超时兜底按 idle 周期再查（捕获刚过 idle 阈值的笔记），不空转
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._idle_fallback_seconds
                )
            except TimeoutError:
                pass
            self._wake_event.clear()
