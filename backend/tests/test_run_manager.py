"""RunManager 后台驱动逻辑：run→（挂起）→resume→终态，事件落库 + 抑制挂起 done + 异常兜底。"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import run_manager as run_manager_module
from app.agent.events import (
    CopilotApprovalEvent,
    CopilotDeltaEvent,
    CopilotDoneEvent,
    CopilotMetaEvent,
)
from app.agent.run import PreparedRun
from app.agent.run_manager import RunHandle, RunManager, is_terminal_state
from app.agent.runtime.context import RunState
from app.schemas.copilot import CopilotRequest
from tests.fakes import FakeCopilotStreamEventRepository


def _prepared() -> PreparedRun:
    return PreparedRun(
        conversation_id=uuid.uuid4(),
        user_message_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
    )


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(approval_timeout_seconds=900.0))


def _handle(prepared: PreparedRun) -> RunHandle:
    return RunHandle(prepared=prepared)


def _meta(prep: PreparedRun) -> CopilotMetaEvent:
    return CopilotMetaEvent(prep.conversation_id, prep.user_message_id, prep.assistant_message_id)


def _approval(prep: PreparedRun) -> CopilotApprovalEvent:
    return CopilotApprovalEvent(
        uuid.uuid4(), str(prep.run_id), "create_note", {}, "新建笔记", "high"
    )


def _manager() -> RunManager:
    return RunManager(make_runtime=lambda session: SimpleNamespace())


async def test_drive_phases_simple_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """无审批：meta→delta→done 顺序落库，run 判 completed。"""
    prepared = _prepared()

    async def fake_stream_run(rt: Any, request: Any, prep: PreparedRun):
        yield _meta(prep)
        yield CopilotDeltaEvent("回答")
        yield CopilotDoneEvent(prep.assistant_message_id)

    monkeypatch.setattr(run_manager_module, "stream_run", fake_stream_run)
    stream_repo = FakeCopilotStreamEventRepository()
    handle = _handle(prepared)
    await _manager()._drive_phases(handle, _runtime(), CopilotRequest(question="x"), stream_repo)

    assert [e.type for e in stream_repo.events] == ["meta", "delta", "done"]
    assert handle.state == RunState.COMPLETED.value


async def test_suspend_suppresses_done_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """挂起审批：approval 落库、挂起 done 被抑制；裁决后 resume 续跑，最终 done 落库。"""
    prepared = _prepared()

    async def fake_stream_run(rt: Any, request: Any, prep: PreparedRun):
        yield _meta(prep)
        yield _approval(prep)
        yield CopilotDoneEvent(prep.assistant_message_id)  # 挂起 done，应被抑制

    async def fake_resume(rt: Any, run_id: str, decisions: Any, cid: Any, aid: Any):
        yield CopilotDeltaEvent("已完成")
        yield CopilotDoneEvent(prepared.assistant_message_id)

    monkeypatch.setattr(run_manager_module, "stream_run", fake_stream_run)
    monkeypatch.setattr(run_manager_module, "resume", fake_resume)
    stream_repo = FakeCopilotStreamEventRepository()
    manager = _manager()

    decisions = [{"approval_id": uuid.uuid4(), "decision": "approve"}]

    async def fake_wait(handle: RunHandle, runtime: Any) -> list[dict[str, Any]]:
        return decisions

    manager._wait_decision = fake_wait  # type: ignore[method-assign]
    handle = _handle(prepared)
    await manager._drive_phases(handle, _runtime(), CopilotRequest(question="x"), stream_repo)

    assert [e.type for e in stream_repo.events] == ["meta", "approval", "delta", "done"]
    assert handle.state == RunState.COMPLETED.value


async def test_drive_phases_error_persists_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """图执行异常：落 error 事件 + run 判 failed。"""
    prepared = _prepared()

    async def fake_stream_run(rt: Any, request: Any, prep: PreparedRun):
        yield _meta(prep)
        raise RuntimeError("boom")

    monkeypatch.setattr(run_manager_module, "stream_run", fake_stream_run)
    stream_repo = FakeCopilotStreamEventRepository()
    handle = _handle(prepared)
    await _manager()._drive_phases(handle, _runtime(), CopilotRequest(question="x"), stream_repo)

    assert [e.type for e in stream_repo.events] == ["meta", "error"]
    assert handle.state == RunState.FAILED.value
    assert stream_repo.events[-1].payload["message"] == "boom"


async def test_drive_phases_decision_timeout_interrupts(monkeypatch: pytest.MonkeyPatch) -> None:
    """审批超时未裁决：run 判 interrupted 收尾（不再续跑）。"""
    prepared = _prepared()

    async def fake_stream_run(rt: Any, request: Any, prep: PreparedRun):
        yield _approval(prep)
        yield CopilotDoneEvent(prep.assistant_message_id)

    monkeypatch.setattr(run_manager_module, "stream_run", fake_stream_run)
    stream_repo = FakeCopilotStreamEventRepository()
    manager = _manager()

    async def fake_wait(handle: RunHandle, runtime: Any) -> None:
        return None

    manager._wait_decision = fake_wait  # type: ignore[method-assign]
    handle = _handle(prepared)
    await manager._drive_phases(handle, _runtime(), CopilotRequest(question="x"), stream_repo)

    assert [e.type for e in stream_repo.events] == ["approval"]
    assert handle.state == RunState.INTERRUPTED.value


def test_is_terminal_state() -> None:
    assert is_terminal_state(RunState.COMPLETED.value)
    assert is_terminal_state(RunState.FAILED.value)
    assert is_terminal_state(RunState.INTERRUPTED.value)
    assert not is_terminal_state(RunState.RUNNING.value)
    assert not is_terminal_state(RunState.SUSPENDED.value)


async def test_cancel_cancels_registered_task() -> None:
    """cancel 取消已登记 run 的后台任务；未登记/无任务返回 False。"""
    manager = _manager()
    prepared = _prepared()
    handle = RunHandle(prepared=prepared)
    handle.task = asyncio.create_task(asyncio.sleep(3600))
    manager._handles[prepared.assistant_message_id] = handle

    assert manager.cancel(prepared.assistant_message_id) is True
    assert manager.cancel(uuid.uuid4()) is False  # 未登记
    with pytest.raises(asyncio.CancelledError):
        await handle.task
