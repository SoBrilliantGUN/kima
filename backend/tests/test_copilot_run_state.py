"""run 生命周期状态（``RunState``）真正被消费：三态写入 + done 审计 + 恢复门禁。"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agent.orchestrate import done_payload, stream_graph
from app.agent.resume import _has_terminal_event, resume
from app.agent.runtime.context import RunState
from app.agent.session import RunSession
from app.models.copilot import CopilotEvent
from app.repositories.approval import InMemoryApprovalStore
from tests.fakes import FakeCopilotEventRepository


def _session() -> RunSession:
    return RunSession(write_tool_names=frozenset())


def _rt(event_repo: FakeCopilotEventRepository | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        db_lock=asyncio.Lock(),
        event_repository=event_repo or FakeCopilotEventRepository(),
        approval_store=InMemoryApprovalStore(),
        runtime=SimpleNamespace(daily_budget=None, approval_timeout_seconds=900.0),
    )


class _ScriptedGraph:
    """按给定 (mode, payload) 序列回放 astream，或直接抛异常。"""

    def __init__(
        self, payloads: list[tuple[str, dict]] | None = None, exc: Exception | None = None
    ):
        self._payloads = payloads or []
        self._exc = exc

    def astream(self, input: object, config: object, stream_mode: object):
        async def gen():
            if self._exc is not None:
                raise self._exc
            for item in self._payloads:
                yield item

        return gen()


def test_done_payload_includes_run_state() -> None:
    rt = SimpleNamespace(model_name="test-model")
    payload = done_payload(rt, uuid.uuid4(), None, "task", RunState.COMPLETED.value)
    assert payload["run_state"] == "completed"


def test_has_terminal_event() -> None:
    repo = FakeCopilotEventRepository()
    run_id = uuid.uuid4()
    # 空日志 → 非终态
    assert not asyncio.run(_has_terminal_event(_rt(repo), run_id))
    # 落一条 done → 终态
    asyncio.run(repo.add_event(CopilotEvent(run_id=run_id, type="done", payload={})))
    assert asyncio.run(_has_terminal_event(_rt(repo), run_id))


async def test_stream_graph_marks_suspended_on_interrupt() -> None:
    session = _session()
    graph = _ScriptedGraph(
        payloads=[
            (
                "updates",
                {
                    "__interrupt__": [
                        SimpleNamespace(
                            value={
                                "tool": "create_note",
                                "args": {},
                                "summary": "",
                                "level": "high",
                            }
                        )
                    ]
                },
            )
        ]
    )
    events = [
        e async for e in stream_graph(
            _rt(), graph, {}, uuid.uuid4(), {}, session, uuid.uuid4(), uuid.uuid4()
        )
    ]
    assert session.run_state == RunState.SUSPENDED.value
    assert len(events) == 1  # 只 yield approval 事件


async def test_stream_graph_marks_completed_on_review_terminal() -> None:
    session = _session()
    graph = _ScriptedGraph(
        payloads=[("updates", {"review": {"review_verdict": "ok", "review_issues": []}})]
    )
    events = [
        e async for e in stream_graph(
            _rt(), graph, {}, uuid.uuid4(), {}, session, uuid.uuid4(), uuid.uuid4()
        )
    ]
    assert session.run_state == RunState.COMPLETED.value
    assert len(events) == 1  # review 事件


async def test_stream_graph_marks_failed_on_exception() -> None:
    session = _session()
    rt = _rt()
    graph = _ScriptedGraph(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        _ = [
            e async for e in stream_graph(
                rt, graph, {}, uuid.uuid4(), {}, session, uuid.uuid4(), uuid.uuid4()
            )
        ]
    assert session.run_state == RunState.FAILED.value
    # 错误事件落库时携带 run_state
    assert rt.event_repository.events[-1].type == "error"
    assert rt.event_repository.events[-1].payload["run_state"] == "failed"


async def test_resume_refuses_terminal_run() -> None:
    """已落 done 事件的 run 不应再续跑（防重复副作用）。"""
    repo = FakeCopilotEventRepository()
    run_id = uuid.uuid4()
    await repo.add_event(CopilotEvent(run_id=run_id, type="done", payload={}))
    rt = _rt(repo)
    events = [
        e
        async for e in resume(
            rt, str(run_id), "approve", uuid.uuid4(), uuid.uuid4()
        )
    ]
    # 短路：只推一段 delta，无 done 事件、无二次副作用
    assert len(events) == 1
    assert getattr(events[0], "text", "") == "该 run 已结束，无需恢复。"
