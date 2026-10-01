"""LLM Planner：DAG 解析 + 拓扑执行 + 失败增量重规划 + 计划状态机（Plan-as-Data）。"""


import pytest

from app.agent.runtime.planner import (
    LLMPlanner,
    Plan,
    PlanStep,
    StepStatus,
    parse_plan,
    validate_plan,
)
from tests.fakes import ScriptedLLM, gateway_run, make_gateway


def test_parse_plan() -> None:
    raw = (
        '```json\n{"steps":[{"id":"1","action":"search","params":{"q":"x"},'
        '"depends_on":[],"terminal":false}]}\n```'
    )
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
    raw = (
        '{"steps":[{"id":"2r","action":"scp","params":{},'
        '"depends_on":["1"],"replaces":"2","terminal":false}]}'
    )
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
                step_id="1",
                action="a",
                params={"x": 1},
                status=StepStatus.COMPLETED,
                output_ref="r1",
            ),
            PlanStep(
                step_id="2",
                action="b",
                depends_on=("1",),
                status=StepStatus.FAILED,
                error="boom",
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
    planner = LLMPlanner(make_gateway(llm=llm))
    async with gateway_run():
        plan = await planner.generate("查点东西", ["search", "read"])
    assert plan.steps == ()
    assert len(llm.calls) == 1


async def test_replan_rejects_hallucinated_tool() -> None:
    llm = ScriptedLLM(['{"steps":[{"id":"2r","action":"nope","params":{}}]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    plan = Plan(steps=(PlanStep(step_id="2", action="transfer", params={}),))
    failed = plan.get_step("2")
    assert failed is not None
    async with gateway_run():
        result = await planner.replan(plan, failed, "boom", ["transfer"])
    assert result == []
