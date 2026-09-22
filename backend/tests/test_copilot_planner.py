"""LLM Planner：DAG 解析 + 拓扑执行 + 失败 replan。"""

from typing import Any

from app.agent.runtime.executor import PlanExecutionError, execute_plan
from app.agent.runtime.planner import LLMPlanner, Plan, PlanStep, parse_plan, validate_plan
from tests.fakes import ScriptedLLM


def test_parse_plan() -> None:
    raw = '```json\n{"steps":[{"id":"1","action":"search","params":{"q":"x"},' \
        '"depends_on":[],"terminal":false}]}\n```'
    plan = parse_plan(raw)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_id == "1"
    assert step.action == "search"
    assert step.params == {"q": "x"}


def test_parse_plan_invalid() -> None:
    assert parse_plan("不是 JSON").steps == ()


def test_parse_plan_rejects_bad_structure() -> None:
    """结构层失败（params 非对象 / steps 非数组 / 缺 id）整体判空，不再静默转空串。"""
    assert parse_plan('{"steps":[{"id":"1","action":"search","params":"oops"}]}').steps == ()
    assert parse_plan('{"steps":"oops"}').steps == ()
    assert parse_plan('{"steps":[{"action":"search"}]}').steps == ()


def test_validate_plan_ok() -> None:
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="search", params={"q": "x"}),
            PlanStep(step_id="2", action="read", params={"id": "y"}, depends_on=("1",)),
        )
    )
    assert validate_plan(plan, ["search", "read"]) == []


def test_validate_plan_catches_hallucinated_tool() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="nope", params={}),))
    errors = validate_plan(plan, ["search", "read"])
    assert errors and any("nope" in e for e in errors)


def test_validate_plan_catches_missing_dep() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="search", depends_on=("9",)),))
    errors = validate_plan(plan, ["search"])
    assert errors and any("9" in e for e in errors)


def test_validate_plan_catches_duplicate_id() -> None:
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="search"),
            PlanStep(step_id="1", action="read"),
        )
    )
    errors = validate_plan(plan, ["search", "read"])
    assert errors and any("重复" in e for e in errors)


async def test_planner_self_corrects_hallucinated_tool() -> None:
    llm = ScriptedLLM(
        [
            '{"steps":[{"id":"1","action":"nope","params":{}}]}',
            '{"steps":[{"id":"1","action":"search","params":{"q":"x"}}]}',
        ]
    )
    planner = LLMPlanner(llm)
    plan = await planner.generate("查点东西", ["search", "read"])
    assert [s.action for s in plan.steps] == ["search"]
    assert len(llm.calls) == 2
    assert "校验失败" in llm.calls[-1][-1].content


async def test_planner_accumulates_last_usage() -> None:
    """自纠错多次调用的 token 用量应累计到 last_usage，供 service 记入预算。"""
    llm = ScriptedLLM(
        [
            '{"steps":[{"id":"1","action":"nope","params":{}}]}',
            '{"steps":[{"id":"1","action":"search","params":{"q":"x"}}]}',
        ],
        prompt_tokens=[100, 200],
        completion_tokens=[10, 20],
    )
    planner = LLMPlanner(llm)
    await planner.generate("查点东西", ["search", "read"])
    assert planner.last_usage.input_tokens == 300
    assert planner.last_usage.output_tokens == 30


async def test_execute_plan_runs_in_order() -> None:
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="search", params={"q": "x"}),
            PlanStep(step_id="2", action="read", params={"id": "y"}, depends_on=("1",)),
        )
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        calls.append((name, params))
        return f"{name}-ok"

    async def replan(plan: Plan, step: PlanStep, error: str) -> list[PlanStep]:
        return []

    results = await execute_plan(plan, run_tool, replan)
    assert calls == [("search", {"q": "x"}), ("read", {"id": "y"})]
    assert results == {"1": "search-ok", "2": "read-ok"}


async def test_execute_plan_replans_on_failure() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="fail", params={}),))

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        if name == "fail":
            raise ValueError("boom")
        return "ok"

    async def replan(plan: Plan, step: PlanStep, error: str) -> list[PlanStep]:
        return [PlanStep(step_id="1r", action="succeed", params={})]

    results = await execute_plan(plan, run_tool, replan)
    assert results == {"1r": "ok"}


async def test_execute_plan_fails_when_no_replan() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="fail", params={}),))

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        raise ValueError("boom")

    async def replan(plan: Plan, step: PlanStep, error: str) -> list[PlanStep]:
        return []

    try:
        await execute_plan(plan, run_tool, replan)
    except PlanExecutionError:
        pass
    else:
        raise AssertionError("应抛 PlanExecutionError")
