"""Copilot run 生命周期与请求解耦：后台任务 + 事件订阅。

POST /chat 同步 ``prepare_run`` 后启动后台任务；后台任务驱动 ``stream_run`` / ``resume``，
把每个 SSE 事件持久化到 ``copilot_stream_events`` 并 ``events.set()`` 唤醒订阅者；订阅端点
（GET /runs/{assistant_message_id}/stream）先按 seq 回放、再 tail 新事件直到终态。run 到终态
后整段删除事件流（最终内容已固化进 ``chat_messages``），并从注册表移除。

``RunManager`` 是 app.state 单例，持有 ``make_runtime``（闭包捕获 app.state 单例 + settings）
以便后台任务独立构造 ``CopilotRuntime``（绕开请求作用域 DI 会话）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.compose import CopilotRuntime
from app.agent.events import CopilotApprovalEvent, CopilotDoneEvent, to_payload
from app.agent.resume import resume
from app.agent.run import PreparedRun, stream_run
from app.agent.runtime.context import RunState
from app.core.db import async_session_factory
from app.models.copilot import CopilotStreamEvent
from app.repositories.copilot import SqlAlchemyCopilotStreamEventRepository
from app.schemas.copilot import CopilotRequest

logger = logging.getLogger(__name__)

# 订阅者心跳/等待超时：挂起等审批或长工具调用时，既保活（防代理断流），又给
# 「清空事件后极窄窗口的 lost-wakeup」一个上界（超时后重新读库判定终态）。
HEARTBEAT_SECONDS = 5.0
# 审批放弃缓冲：超过审批超时 + 缓冲仍未裁决，run 判 interrupted 收尾（防后台任务永挂）。
_APPROVAL_GRACE_SECONDS = 60.0
_TERMINAL_STATES = frozenset(
    {RunState.COMPLETED.value, RunState.FAILED.value, RunState.INTERRUPTED.value}
)


@dataclass
class RunHandle:
    """一次后台 run 的注册表条目：ids + 订阅信号 + 挂起后的裁决 future。"""

    prepared: PreparedRun
    state: str = RunState.RUNNING.value
    task: asyncio.Task[None] | None = None
    # 订阅者等待新事件的信号（生产者每落一条事件 set 一次，不清空，靠重新读库判定）
    events: asyncio.Event = field(default_factory=asyncio.Event)
    # 挂起后等审批裁决：approve 端点 set 这个 future，后台任务 await 它续跑
    resume_future: asyncio.Future[list[dict[str, Any]] | None] | None = None

    @property
    def assistant_message_id(self) -> uuid.UUID:
        return self.prepared.assistant_message_id

    @property
    def run_id(self) -> uuid.UUID:
        return self.prepared.run_id

    @property
    def conversation_id(self) -> uuid.UUID:
        return self.prepared.conversation_id


class RunManager:
    """后台 run 注册表 + 订阅信号中枢（app.state 单例）。"""

    def __init__(self, make_runtime: Callable[[AsyncSession], CopilotRuntime]) -> None:
        self._make_runtime = make_runtime
        self._handles: dict[uuid.UUID, RunHandle] = {}

    def get(self, assistant_message_id: uuid.UUID) -> RunHandle | None:
        return self._handles.get(assistant_message_id)

    def start(self, prepared: PreparedRun, request: CopilotRequest) -> RunHandle:
        """启动后台任务驱动一轮 run，并登记进注册表。"""
        handle = RunHandle(prepared=prepared)
        handle.task = asyncio.create_task(self._drive(handle, request))
        self._handles[prepared.assistant_message_id] = handle
        return handle

    def submit_decision(
        self, assistant_message_id: uuid.UUID, decisions: list[dict[str, Any]]
    ) -> bool:
        """approve 端点回填裁决，唤醒挂起的后台任务；无挂起 run 或已裁决则返回 False。"""
        handle = self._handles.get(assistant_message_id)
        if handle is None or handle.resume_future is None or handle.resume_future.done():
            return False
        handle.resume_future.set_result(decisions)
        return True

    def cancel(self, assistant_message_id: uuid.UUID) -> bool:
        """前端「停止」：取消后台 run（触发 run_reactive finally 回填部分内容），返回是否取消到。"""
        handle = self._handles.get(assistant_message_id)
        if handle is None or handle.task is None:
            return False
        handle.task.cancel()
        return True

    async def _drive(self, handle: RunHandle, request: CopilotRequest) -> None:
        try:
            async with async_session_factory() as session:
                runtime = self._make_runtime(session)
                async with async_session_factory() as stream_session:
                    stream_repo = SqlAlchemyCopilotStreamEventRepository(stream_session)
                    await self._drive_phases(handle, runtime, request, stream_repo)
                    # 终态：事件流整段删除（最终内容已固化进 chat_messages）
                    await stream_repo.delete_events(handle.assistant_message_id)
        except asyncio.CancelledError:
            # 前端停止：run_reactive 的 finally 已回填部分内容（run_state=interrupted）；
            # 这里收尾删流 + 终态，再 re-raise 让任务以 cancelled 收尾。
            handle.state = RunState.INTERRUPTED.value
            await self._delete_stream_events(handle)
            raise
        except Exception:  # noqa: BLE001 - 会话装配失败等兜底，不裸抛
            logger.exception("[copilot] 后台 run 异常 run_id=%s", handle.run_id)
            handle.state = RunState.FAILED.value
        finally:
            handle.events.set()  # 唤醒仍在 tail 的订阅者，让它读到终态后关闭
            self._handles.pop(handle.assistant_message_id, None)

    async def _delete_stream_events(self, handle: RunHandle) -> None:
        """取消后清理事件流（独立短会话，best-effort，失败不阻断取消收尾）。"""
        try:
            async with async_session_factory() as session:
                repo = SqlAlchemyCopilotStreamEventRepository(session)
                await repo.delete_events(handle.assistant_message_id)
        except Exception:  # noqa: BLE001
            logger.warning("[copilot] 取消后清理事件流失败（忽略）", exc_info=True)

    async def _drive_phases(
        self,
        handle: RunHandle,
        runtime: CopilotRuntime,
        request: CopilotRequest,
        stream_repo: SqlAlchemyCopilotStreamEventRepository,
    ) -> None:
        """驱动 run →（挂起）→ resume → … 直到终态；每阶段边流边落库 + notify。"""
        phase = "run"
        decisions: list[dict[str, Any]] | None = []
        try:
            while True:
                saw_approval = False
                if phase == "run":
                    generator = stream_run(runtime, request, handle.prepared)
                else:
                    assert decisions is not None  # resume 阶段必有裁决（None 分支已 return）
                    generator = resume(
                        runtime,
                        str(handle.run_id),
                        decisions,
                        handle.conversation_id,
                        handle.assistant_message_id,
                    )
                async for event in generator:
                    if isinstance(event, CopilotApprovalEvent):
                        saw_approval = True
                    if isinstance(event, CopilotDoneEvent) and saw_approval:
                        # 挂起阶段的 done（run_state=suspended）是阶段边界，不是终态，抑制之
                        continue
                    type_, payload = to_payload(event)
                    await self._persist(stream_repo, handle, type_, payload)
                if not saw_approval:
                    handle.state = RunState.COMPLETED.value
                    return
                # 挂起：等审批裁决；超时（审批超时 + 缓冲）判 interrupted 收尾
                handle.state = RunState.SUSPENDED.value
                decisions = await self._wait_decision(handle, runtime)
                if decisions is None:
                    handle.state = RunState.INTERRUPTED.value
                    return
                phase = "resume"
        except Exception as exc:  # noqa: BLE001 - 图执行异常：落 error 事件 + 终态
            logger.exception("[copilot] 后台 run 图执行异常 run_id=%s", handle.run_id)
            handle.state = RunState.FAILED.value
            await self._persist(
                stream_repo,
                handle,
                "error",
                {"code": "internal_error", "message": str(exc)},
            )

    async def _wait_decision(
        self, handle: RunHandle, runtime: CopilotRuntime
    ) -> list[dict[str, Any]] | None:
        handle.resume_future = asyncio.get_running_loop().create_future()
        timeout = runtime.runtime.approval_timeout_seconds + _APPROVAL_GRACE_SECONDS
        try:
            return await asyncio.wait_for(handle.resume_future, timeout=timeout)
        except TimeoutError:
            logger.warning("[copilot] 审批超时未裁决，run 收尾 run_id=%s", handle.run_id)
            return None
        finally:
            handle.resume_future = None

    async def _persist(
        self,
        stream_repo: SqlAlchemyCopilotStreamEventRepository,
        handle: RunHandle,
        type_: str,
        payload: dict[str, Any],
    ) -> None:
        await stream_repo.add_event(
            CopilotStreamEvent(
                run_id=handle.run_id,
                assistant_message_id=handle.assistant_message_id,
                type=type_,
                payload=payload,
            )
        )
        handle.events.set()


def is_terminal_state(state: str) -> bool:
    """run 状态是否终态（completed/failed/interrupted）——事件流可删、订阅可关。"""
    return state in _TERMINAL_STATES
