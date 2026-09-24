"""Copilot 工具集的模块级 helper 与参数契约（供 ``build_tools`` 复用）。"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import Any, TypeVar, cast

from app.agent.toolmeta import LATENCY_TIMEOUT_FACTOR, ParamContract
from app.models.copilot import MemoryKind
from app.services.knowledge_base import KnowledgeBaseService

WEB_TOP_K = 5
LIST_LIMIT_MAX = 100
LIST_LIMIT_DEFAULT = 50

# —— 下行参数契约（防线② 参数级校验，执行前由 Loop 统一拦截）——
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MEMORY_KINDS = frozenset({"constraint", "procedural", "semantic", "episodic"})
LIST_PARAM_CONTRACT = ParamContract(
    min={"limit": 1, "offset": 0}, max={"limit": LIST_LIMIT_MAX}
)
MEMORY_KIND_CONTRACT = ParamContract(enum={"kind": _MEMORY_KINDS})
PROFILE_KIND_CONTRACT = ParamContract(enum={"kind": frozenset({"soul", "user"})})
DOC_ID_CONTRACT = ParamContract(pattern={"document_id": _UUID_PATTERN})
NOTE_ID_CONTRACT = ParamContract(pattern={"note_id": _UUID_PATTERN})


Fn = TypeVar("Fn", bound=Callable[..., Awaitable[Any]])


def serialize(lock: asyncio.Lock) -> Callable[[Fn], Fn]:
    """把工具函数串行化。

    LangGraph 的 ToolNode 对同一轮的多工具调用用 `asyncio.gather` 并发执行，而所有
    工具共享同一个 AsyncSession（每请求一个，见 deps.py）。SQLAlchemy 的 AsyncSession
    非并发安全：并发写同一 session 时，可能撞上 commit 的中间态，抛出
    "This session is in 'prepared' state; no further SQL can be emitted..."。
    故用一把锁（每请求一把，见 build_tools）把工具的 DB 访问串起来。
    """

    def decorator(fn: Fn) -> Fn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with lock:
                return await fn(*args, **kwargs)

        return cast(Fn, wrapper)

    return decorator


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def timeout_s(latency_ms: int) -> float:
    """超时上限 = 预计延迟 × 系数（反馈契约「超时 = 延迟 × 3」）。"""
    return latency_ms * LATENCY_TIMEOUT_FACTOR / 1000.0


def with_has_more(lines: list[str], offset: int, total: int, label: str) -> str:
    """列表分页提示：还有更多时在末尾追加一行提示（资源契约：分页而非静默截断）。"""
    shown = offset + len(lines)
    if shown < total:
        lines.append(f"…（还有 {total - shown} 个{label}未显示，用 offset={shown} 继续列出）")
    return "\n".join(lines)


async def parse_kb_ids(
    kb_ids: Sequence[str] | None, kb_service: KnowledgeBaseService
) -> list[uuid.UUID]:
    if kb_ids:
        parsed = [i for i in (parse_uuid(k) for k in kb_ids) if i is not None]
        if parsed:
            return parsed
    items, _ = await kb_service.list(limit=100, offset=0)
    return [kb.id for kb in items]


def parse_kind(kind: str | None) -> MemoryKind | None:
    if kind is None:
        return None
    try:
        return MemoryKind(kind)
    except ValueError:
        return None
