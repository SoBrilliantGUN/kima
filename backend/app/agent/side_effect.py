"""写工具副作用确定性对账（DB 回查实现）。

`DbSideEffectVerifier` 实现 `guardrail.review.SideEffectVerifier` 协议：只针对「工具
声称成功（返回了 id）但副作用未落库」这类伪造回查 note/memory——若工具明确返回失败
（「创建失败」「kind 必须」）则跳过，交给 LLM 审查器做声称 vs 轨迹的一致性判断。
"""

import asyncio
import re
import uuid
from typing import Any

from app.core.exceptions import DomainError
from app.services.copilot import CopilotMemoryService
from app.services.note import NoteService

# 写工具返回串里的落库 id（create_note / write_memory）
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class DbSideEffectVerifier:
    """写工具副作用确定性对账：回查 note/memory 确认声称成功的写入真的持久化。"""

    def __init__(
        self,
        note_service: NoteService,
        memory_service: CopilotMemoryService,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._note_service = note_service
        self._memory_service = memory_service
        self._lock = lock

    async def verify(self, tool_name: str, args: dict[str, Any], result: str) -> str | None:
        if tool_name == "create_note":
            return await self._verify_note(result)
        if tool_name == "write_memory":
            return await self._verify_memory(result)
        if tool_name == "update_profile":
            return None  # soul/user 文件写入，不做 DB 对账
        return None

    async def _verify_note(self, result: str) -> str | None:
        match = _UUID_RE.search(result)
        if match is None:
            return None  # 失败（红绿灯文本无 id）交给 LLM 审查器
        try:
            await self._note_service.get(uuid.UUID(match.group(0)))
        except DomainError:
            return "create_note 返回了 id，但笔记未真正落库（副作用缺失）"
        return None

    async def _verify_memory(self, result: str) -> str | None:
        match = _UUID_RE.search(result)
        if match is None:
            return None  # 失败（红绿灯文本无 id）交给 LLM 审查器
        async with self._lock if self._lock is not None else _null_async_ctx():
            memory = await self._memory_service.get_memory(uuid.UUID(match.group(0)))
        if memory is None:
            return "write_memory 返回了 id，但记忆未真正落库（副作用缺失）"
        return None


class _null_async_ctx:
    """无锁时的空异步上下文管理器占位。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None
