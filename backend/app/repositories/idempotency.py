"""写工具幂等去重的数据访问层：Protocol + SQLAlchemy + 内存实现。

幂等键标识「一次业务意图」（编排层注入的内容派生键，而非位置序号），执行前原子抢占
（processing），成功后落结果缓存（succeeded），永久失败落错误（failed_final），
跨重试/崩溃复用同一个键。

三态简化说明：文章四态里的 ``failed_retryable`` 由工具层 ``with_retry`` 在内存兜底
（瞬态错误不落表、直接退避重试），幂等表只记录 processing/succeeded/failed_final。

``claim`` 是原子抢占原语：``INSERT ... ON CONFLICT DO NOTHING`` 抢到则返回 inserted，
抢不到则读已有记录按 request_hash/status/expires_at 分流（冲突拒绝 / 命中缓存 / 处理中 /
永久失败）。processing 过期（崩溃在 claim 后 succeed 前留下的孤儿）由 claim 惰性回收。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.copilot import CopilotIdempotency, IdempotencyStatus

# 幂等记录默认 TTL（秒）：processing 残留回收 + 结果缓存过期埋点（无 sweep，惰性回收）。
DEFAULT_TTL_SECONDS = 86400  # 24h


class IdempotencyOutcome(StrEnum):
    """claim 的分流结局：inserted 抢到执行权 / cached 命中缓存 / conflict 同键不同参数 /
    processing 处理中稍后重试 / failed_final 命中永久失败。"""

    INSERTED = "inserted"
    CACHED = "cached"
    CONFLICT = "conflict"
    PROCESSING = "processing"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True)
class IdempotencyClaim:
    """claim 的返回：outcome 决定工具体走哪条分支；response/error 仅 cached/failed_final 携带。"""

    outcome: IdempotencyOutcome
    response: str | None = None
    error: str | None = None


class IdempotencyStore(Protocol):
    """幂等存取原语（可注入内存 Fake 做确定性测试）。"""

    async def claim(self, tool_name: str, idem_key: str, request_hash: str) -> IdempotencyClaim: ...

    async def succeed(self, tool_name: str, idem_key: str, response: str) -> None: ...

    async def fail(self, tool_name: str, idem_key: str, error: str) -> None: ...


def _expired(record: CopilotIdempotency, now: datetime) -> bool:
    """processing 记录是否已过 TTL（崩溃残留孤儿）。"""
    return record.expires_at is not None and record.expires_at <= now


def _resolve(record: CopilotIdempotency, request_hash: str) -> IdempotencyClaim:
    """把已存在的幂等记录按「request_hash 冲突 → 成功缓存 → 永久失败 → 处理中」分流。"""
    if record.request_hash != request_hash:
        # 同键不同参数：LLM 改了参数却复用旧键，返回缓存会静默给错结果，必须拒绝（§5.4）。
        return IdempotencyClaim(IdempotencyOutcome.CONFLICT)
    if record.status is IdempotencyStatus.SUCCEEDED:
        return IdempotencyClaim(IdempotencyOutcome.CACHED, response=record.response)
    if record.status is IdempotencyStatus.FAILED_FINAL:
        return IdempotencyClaim(IdempotencyOutcome.FAILED_FINAL, error=record.error)
    # processing（未过期）：并发或正在执行，让调用方稍后重试
    return IdempotencyClaim(IdempotencyOutcome.PROCESSING)


class SqlAlchemyIdempotencyStore:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._ttl_seconds = ttl_seconds

    async def claim(self, tool_name: str, idem_key: str, request_hash: str) -> IdempotencyClaim:
        """原子抢占：插入 processing；冲突则读记录分流（含 processing 过期惰性回收）。"""
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._ttl_seconds)
        async with self._session_factory() as session:
            inserted = cast(
                CursorResult[Any],
                await session.execute(
                    pg_insert(CopilotIdempotency)
                    .values(
                        tool_name=tool_name,
                        idem_key=idem_key,
                        request_hash=request_hash,
                        status=IdempotencyStatus.PROCESSING,
                        expires_at=expires,
                    )
                    .on_conflict_do_nothing(index_elements=["tool_name", "idem_key"])
                ),
            )
            await session.commit()
            if inserted.rowcount == 1:
                return IdempotencyClaim(IdempotencyOutcome.INSERTED)
            record = await session.get(CopilotIdempotency, (tool_name, idem_key))
            if record is None:  # 理论不可达：冲突必有一条已落库
                return IdempotencyClaim(IdempotencyOutcome.INSERTED)
            # processing 过期 → 崩溃残留孤儿，原子回收为己用后重执行（抢占仍靠唯一约束兜底）
            if record.status is IdempotencyStatus.PROCESSING and _expired(record, now):
                reclaimed = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(CopilotIdempotency)
                        .where(
                            CopilotIdempotency.tool_name == tool_name,
                            CopilotIdempotency.idem_key == idem_key,
                            CopilotIdempotency.status == IdempotencyStatus.PROCESSING,
                            CopilotIdempotency.expires_at <= now,
                        )
                        .values(request_hash=request_hash, expires_at=expires)
                    ),
                )
                await session.commit()
                if reclaimed.rowcount == 1:
                    return IdempotencyClaim(IdempotencyOutcome.INSERTED)
                # 被并发抢占，重读再分流（罕见）
                record = await session.get(CopilotIdempotency, (tool_name, idem_key))
                if record is None:
                    return IdempotencyClaim(IdempotencyOutcome.INSERTED)
            return _resolve(record, request_hash)

    async def succeed(self, tool_name: str, idem_key: str, response: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(CopilotIdempotency)
                .where(
                    CopilotIdempotency.tool_name == tool_name,
                    CopilotIdempotency.idem_key == idem_key,
                )
                .values(status=IdempotencyStatus.SUCCEEDED, response=response)
            )
            await session.commit()

    async def fail(self, tool_name: str, idem_key: str, error: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(CopilotIdempotency)
                .where(
                    CopilotIdempotency.tool_name == tool_name,
                    CopilotIdempotency.idem_key == idem_key,
                )
                .values(status=IdempotencyStatus.FAILED_FINAL, error=error)
            )
            await session.commit()


class InMemoryIdempotencyStore:
    """内存版幂等去重（测试/无 DB 场景）：dict 存取，逻辑与 SQLAlchemy 版同构。

    单事件循环 + 工具执行已被 ``serialize(lock)`` 串行化，claim 的检查-写入无需额外锁。
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._rows: dict[tuple[str, str], CopilotIdempotency] = {}
        self._ttl_seconds = ttl_seconds

    async def claim(self, tool_name: str, idem_key: str, request_hash: str) -> IdempotencyClaim:
        now = datetime.now(UTC)
        key = (tool_name, idem_key)
        record = self._rows.get(key)
        if record is None:
            self._rows[key] = CopilotIdempotency(
                tool_name=tool_name,
                idem_key=idem_key,
                request_hash=request_hash,
                status=IdempotencyStatus.PROCESSING,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            return IdempotencyClaim(IdempotencyOutcome.INSERTED)
        if record.status is IdempotencyStatus.PROCESSING and _expired(record, now):
            record.request_hash = request_hash
            record.expires_at = now + timedelta(seconds=self._ttl_seconds)
            return IdempotencyClaim(IdempotencyOutcome.INSERTED)
        return _resolve(record, request_hash)

    async def succeed(self, tool_name: str, idem_key: str, response: str) -> None:
        record = self._rows.get((tool_name, idem_key))
        if record is not None:
            record.status = IdempotencyStatus.SUCCEEDED
            record.response = response

    async def fail(self, tool_name: str, idem_key: str, error: str) -> None:
        record = self._rows.get((tool_name, idem_key))
        if record is not None:
            record.status = IdempotencyStatus.FAILED_FINAL
            record.error = error
