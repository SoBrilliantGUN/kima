"""resume 的 plan run 预算对齐：按 plan_created 事件步数补 scale 资源预算。"""

import uuid
from types import SimpleNamespace
from typing import Any

from app.agent.resume import _plan_step_count
from app.models.copilot import CopilotEvent
from tests.fakes import FakeCopilotEventRepository


async def test_plan_step_count_returns_steps() -> None:
    """plan_created 事件带 steps → 返回步数（供 scale_budget_for_plan）。"""
    repo = FakeCopilotEventRepository()
    run_id = uuid.uuid4()
    await repo.add_event(
        CopilotEvent(
            run_id=run_id,
            type="plan_created",
            payload={
                "version": 1,
                "steps": [
                    {"step_id": "1", "action": "a", "depends_on": []},
                    {"step_id": "2", "action": "b", "depends_on": []},
                    {"step_id": "3", "action": "c", "depends_on": []},
                ],
            },
        )
    )
    rt: Any = SimpleNamespace(event_repository=repo)
    assert await _plan_step_count(rt, run_id) == 3


async def test_plan_step_count_none_for_reactive_run() -> None:
    """无 plan_created 事件（reactive run）→ 返回 None，不 scale 预算。"""
    repo = FakeCopilotEventRepository()
    run_id = uuid.uuid4()
    await repo.add_event(CopilotEvent(run_id=run_id, type="tool_call", payload={}))
    rt: Any = SimpleNamespace(event_repository=repo)
    assert await _plan_step_count(rt, run_id) is None
