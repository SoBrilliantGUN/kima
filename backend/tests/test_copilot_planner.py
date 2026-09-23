"""LLM Planner：DAG 解析 + 拓扑执行 + 失败增量重规划 + 计划状态机（Plan-as-Data）。"""

import uuid
from typing import Any

import pytest

from app.agent.gateway import LLMGateway
from app.agent.runtime.executor import PlanExecutionError, execute_plan
from app.agent.runtime.planner import (
    LLMPlanner,
    Plan,
    PlanStep,
    StepStatus,
    parse_plan,
    validate_plan,
)
from app.repositories.plan import InMemoryPlanStore
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


def test_parse_plan_replaces_field() -> None:
    """replaces 声明映射到 replaces_step_id（增量重规划的「替代身份证」）。"""
    raw = '{"steps":[{"id":"2r","action":"scp","params":{},' \
        '"depends_on":["1"],"replaces":"2","terminal":false}]}'
    step = parse_plan(raw).steps[0]
    assert step.replaces_step_id == "2"
    assert step.depends_on == ("1",)


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


def test_parse_plan_rejects_duplicate_id() -> None:
    """重复 id 在构造 Plan（dict 去重）之前于解析边界拦截。"""
    assert parse_plan('{"steps":[{"id":"1","action":"a"},{"id":"1","action":"b"}]}').steps == ()


def test_parse_plan_rejects_empty_id() -> None:
    assert parse_plan('{"steps":[{"id":"","action":"a"}]}').steps == ()


def test_validate_plan_catches_cycle() -> None:
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="a", depends_on=("2",)),
            PlanStep(step_id="2", action="b", depends_on=("1",)),
        )
    )
    errors = validate_plan(plan, ["a", "b"])
    assert errors and any("循环" in e for e in errors)


def test_mark_downstream_obsolete() -> None:
    """失败步骤下游作废，已完成步骤保留，失败步骤本身保持 FAILED。"""
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="a"),
            PlanStep(step_id="2", action="b", depends_on=("1",)),
            PlanStep(step_id="3", action="c", depends_on=("2",)),
            PlanStep(step_id="4", action="d", depends_on=("2",)),
        )
    )
    s1 = plan.get_step("1")
    s2 = plan.get_step("2")
    assert s1 is not None and s2 is not None
    s1.status = StepStatus.COMPLETED
    s2.status = StepStatus.FAILED
    obsolete = plan.mark_downstream_obsolete("2")
    assert set(obsolete) == {"3", "4"}
    assert s1.status == StepStatus.COMPLETED  # 已完成不标记（工程资产）
    assert s2.status == StepStatus.FAILED  # 失败步骤保持 FAILED


def test_merge_defensively_obsoletes_replaced_step() -> None:
    """LLM 不按套路出牌时，merge 强制把被替换的已完成步骤打成 OBSOLETE。"""
    plan = Plan(steps=(PlanStep(step_id="1", action="dump", params={}),))
    s1 = plan.get_step("1")
    assert s1 is not None
    s1.status = StepStatus.COMPLETED
    plan.merge([PlanStep(step_id="1r", action="redump", params={}, replaces_step_id="1")])
    assert s1.status == StepStatus.OBSOLETE
    assert plan.version == 2
    nr = plan.get_step("1r")
    assert nr is not None and nr.status == StepStatus.PENDING and nr.version_created == 2


def test_merge_rejects_missing_dep() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    with pytest.raises(ValueError):
        plan.merge([PlanStep(step_id="2", action="b", depends_on=("9",))])


def test_merge_rejects_cycle() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    with pytest.raises(ValueError):
        plan.merge(
            [
                PlanStep(step_id="2", action="b", depends_on=("3",)),
                PlanStep(step_id="3", action="c", depends_on=("2",)),
            ]
        )


def test_plan_to_from_dict_roundtrip() -> None:
    plan = Plan(
        steps=(
            PlanStep(
                step_id="1", action="a", params={"x": 1},
                status=StepStatus.COMPLETED, output_ref="r1",
            ),
            PlanStep(
                step_id="2", action="b", depends_on=("1",),
                status=StepStatus.FAILED, error="boom",
            ),
        )
    )
    plan.version = 3
    restored = Plan.from_dict(plan.to_dict())
    assert restored.version == 3
    assert [s.step_id for s in restored.steps] == ["1", "2"]
    s1 = restored.get_step("1")
    s2 = restored.get_step("2")
    assert s1 is not None and s1.status == StepStatus.COMPLETED and s1.output_ref == "r1"
    assert s2 is not None and s2.error == "boom" and s2.depends_on == ("1",)


async def test_planner_fail_closed_on_hallucinated_tool() -> None:
    llm = ScriptedLLM(['{"steps":[{"id":"1","action":"nope","params":{}}]}'])
    planner = LLMPlanner(LLMGateway(llm=llm))
    plan = await planner.generate("查点东西", ["search", "read"])
    assert plan.steps == ()
    assert len(llm.calls) == 1


async def test_replan_rejects_hallucinated_tool() -> None:
    llm = ScriptedLLM(['{"steps":[{"id":"2r","action":"nope","params":{}}]}'])
    planner = LLMPlanner(LLMGateway(llm=llm))
    plan = Plan(steps=(PlanStep(step_id="2", action="transfer", params={}),))
    failed = plan.get_step("2")
    assert failed is not None
    result = await planner.replan(plan, failed, "boom", ["transfer"])
    assert result == []


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

    async def replan(plan: Plan, step: PlanStep, error: str, tool_names: Any) -> list[PlanStep]:
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

    async def replan(plan: Plan, step: PlanStep, error: str, tool_names: Any) -> list[PlanStep]:
        return [PlanStep(step_id="1r", action="succeed", params={})]

    results = await execute_plan(plan, run_tool, replan)
    assert results == {"1r": "ok"}


async def test_execute_plan_fails_when_no_replan() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="fail", params={}),))

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        raise ValueError("boom")

    async def replan(plan: Plan, step: PlanStep, error: str, tool_names: Any) -> list[PlanStep]:
        return []

    try:
        await execute_plan(plan, run_tool, replan)
    except PlanExecutionError:
        pass
    else:
        raise AssertionError("应抛 PlanExecutionError")


async def test_execute_plan_replaces_downstream_not_orphaned() -> None:
    """核心 bug 修复：失败步骤下游不再孤儿化，替换步骤带 replaces + 完整依赖继续走。"""
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="dump", params={}),
            PlanStep(step_id="2", action="transfer", params={}, depends_on=("1",)),
            PlanStep(step_id="3", action="verify", params={}, depends_on=("2",)),
        )
    )
    calls: list[str] = []

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        calls.append(name)
        if name == "transfer":
            raise ValueError("firewall blocked")
        return f"{name}-ok"

    async def replan(plan: Plan, step: PlanStep, error: str, tool_names: Any) -> list[PlanStep]:
        assert step.step_id == "2"
        return [
            PlanStep(
                step_id="2r", action="scp", params={},
                depends_on=("1",), replaces_step_id="2",
            ),
            PlanStep(
                step_id="3r", action="verify", params={},
                depends_on=("2r",), replaces_step_id="3",
            ),
        ]

    results = await execute_plan(plan, run_tool, replan)
    # S1 只 dump 一次（不重复副作用）；S2 失败被 S2' 替换；S3 作废被 S3' 替换
    assert calls == ["dump", "transfer", "scp", "verify"]
    assert results == {"1": "dump-ok", "2r": "scp-ok", "3r": "verify-ok"}


async def test_execute_plan_resumes_from_checkpoint() -> None:
    """崩溃恢复：从序列化快照恢复后跳过已完成步骤，只跑 PENDING。"""
    plan = Plan(
        steps=(
            PlanStep(
                step_id="1", action="dump", params={},
                status=StepStatus.COMPLETED, output_ref="dump-ok",
            ),
            PlanStep(step_id="2", action="verify", params={}, depends_on=("1",)),
        )
    )
    restored = Plan.from_dict(plan.to_dict())  # 模拟 checkpoint 落库 + 恢复
    calls: list[str] = []

    async def run_tool(name: str, params: dict[str, Any]) -> str:
        calls.append(name)
        return f"{name}-ok"

    async def replan(plan: Plan, step: PlanStep, error: str, tool_names: Any) -> list[PlanStep]:
        return []

    results = await execute_plan(restored, run_tool, replan)
    assert calls == ["verify"]  # S1 已 COMPLETED，不重跑
    assert results == {"2": "verify-ok"}


async def test_plan_store_save_load_roundtrip() -> None:
    """检查点存储的扁平结构 {task, version, steps} 存取往返。"""
    store = InMemoryPlanStore()
    run_id = uuid.uuid4()
    plan = Plan(
        steps=(
            PlanStep(
                step_id="1", action="a", params={},
                status=StepStatus.COMPLETED, output_ref="r1",
            ),
        )
    )
    await store.save(run_id, {"task": "任务", **plan.to_dict()})
    loaded = await store.load(run_id)
    assert loaded is not None and loaded["task"] == "任务"
    restored = Plan.from_dict(loaded)
    s1 = restored.get_step("1")
    assert s1 is not None and s1.status == StepStatus.COMPLETED and s1.output_ref == "r1"
