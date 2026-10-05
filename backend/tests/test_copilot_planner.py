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
    plan.merge(
        [PlanStep(step_id="1r", action="redump", params={}, replaces_step_id="1")],
        ["dump", "redump"],
    )
    assert s1.status == StepStatus.OBSOLETE
    assert plan.version == 2
    nr = plan.get_step("1r")
    assert nr is not None and nr.status == StepStatus.PENDING and nr.version_created == 2


def test_merge_same_id_reuse_gets_renamed_not_overwritten() -> None:
    """同名复用（id == replaces）不再覆盖旧步骤：旧步骤保持 OBSOLETE，新步骤改名 #2。

    这是「节点被替换多次也能区分」的关键：同名替换走系统改名，旧版本条目仍在。
    """
    plan = Plan(steps=(PlanStep(step_id="1", action="dump", params={"x": 1}),))
    old = plan.get_step("1")
    assert old is not None
    old.status = StepStatus.COMPLETED
    plan.merge(
        [PlanStep(step_id="1", action="redump", params={"x": 2}, replaces_step_id="1")],
        ["dump", "redump"],
    )
    assert plan.version == 2
    # 旧步骤仍在，只是作废（未被覆盖）
    assert old is not None and old.status == StepStatus.OBSOLETE
    assert old.action == "dump" and old.params == {"x": 1}
    # 新步骤拿到系统分配的独立 id
    renamed = plan.get_step("1#2")
    assert renamed is not None
    assert renamed.action == "redump" and renamed.replaces_step_id == "1"
    assert renamed.version_created == 2


def test_merge_multiple_replacements_are_distinguishable() -> None:
    """同一节点连续替换多次：每次都是独立条目，靠 replaces_step_id 串成链。"""
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    plan.merge([PlanStep(step_id="2", action="b", replaces_step_id="1")], ["a", "b"])
    plan.merge([PlanStep(step_id="3", action="c", replaces_step_id="2")], ["a", "b", "c"])
    ids = {s.step_id for s in plan.steps}
    assert ids == {"1", "2", "3"}
    s1 = plan.get_step("1")
    s2 = plan.get_step("2")
    assert s1 is not None and s1.status == StepStatus.OBSOLETE
    assert s2 is not None and s2.status == StepStatus.OBSOLETE
    c = plan.get_step("3")
    assert c is not None and c.replaces_step_id == "2" and c.status == StepStatus.PENDING
    # 三次合并分别落在 version 2/3/4
    assert {s.version_created for s in plan.steps} == {1, 2, 3}


def test_merge_renames_collision_with_unrelated_step() -> None:
    """新步骤 id 撞上已有步骤（且非替换它）时改名，而不是报错丢弃整批。"""
    plan = Plan(steps=(PlanStep(step_id="1", action="a"), PlanStep(step_id="2", action="b")))
    plan.merge([PlanStep(step_id="2", action="c", depends_on=("1",))], ["a", "b", "c"])
    assert {s.step_id for s in plan.steps} == {"1", "2", "2#2"}
    renamed = plan.get_step("2#2")
    assert renamed is not None and renamed.depends_on == ("1",)


def test_merge_renames_intrabatch_dep_follows() -> None:
    """批内 depends_on 引用被改名的步骤时，引用应跟随新 id。"""
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    plan.merge(
        [
            PlanStep(step_id="1", action="b", replaces_step_id="1"),
            PlanStep(step_id="2", action="c", depends_on=("1",)),
        ],
        ["a", "b", "c"],
    )
    # 批内 "1" 被改名为 "1#2"，依赖它的 "2" 应指向 "1#2" 而非旧 "1"
    c = plan.get_step("2")
    assert c is not None and c.depends_on == ("1#2",)


def test_merge_rejects_missing_dep() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    with pytest.raises(ValueError):
        plan.merge([PlanStep(step_id="2", action="b", depends_on=("9",))], ["a", "b"])


def test_merge_rejects_cycle() -> None:
    plan = Plan(steps=(PlanStep(step_id="1", action="a"),))
    with pytest.raises(ValueError):
        plan.merge(
            [
                PlanStep(step_id="2", action="b", depends_on=("3",)),
                PlanStep(step_id="3", action="c", depends_on=("2",)),
            ],
            ["a", "b", "c"],
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
    assert len(llm.calls) == 3


async def test_replan_passes_tool_validation_to_merge() -> None:
    """幻觉工具不再由 replan 拦截，而是由 merge 在并入边界抛 ValueError。"""
    llm = ScriptedLLM(['{"steps":[{"id":"2r","action":"nope","params":{}}]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    plan = Plan(steps=(PlanStep(step_id="2", action="transfer", params={}),))
    failed = plan.get_step("2")
    assert failed is not None
    async with gateway_run():
        result = await planner.replan(plan, failed, "boom", ["transfer"])
    # replan 只做结构解析，幻觉工具放行
    assert len(result) == 1 and result[0].action == "nope"
    # 并入边界（merge）拦截
    with pytest.raises(ValueError):
        plan.merge(result, ["transfer"])


async def test_planner_self_corrects_after_feedback() -> None:
    """解析/语义失败时把精确错误回喂，第二次生成成功（自纠错链路生效）。"""
    llm = ScriptedLLM(
        [
            '{"steps":[{"id":"1","action":"nope","params":{}}]}',  # 幻觉工具 → 回喂
            '{"steps":[{"id":"1","action":"search","params":{}}]}',  # 修正后合法
        ]
    )
    planner = LLMPlanner(make_gateway(llm=llm))
    async with gateway_run():
        plan = await planner.generate("查点东西", ["search", "read"])
    assert len(plan.steps) == 1
    assert plan.steps[0].action == "search"
    assert len(llm.calls) == 2
    # 第二次调用把第一次的精确错误回喂进了 prompt
    assert "nope" in "".join(m.content for m in llm.calls[1])


async def test_planner_generate_injects_skills() -> None:
    """skill 全文作为「技能指引」段拼进规划器 user prompt（约束之前）。"""
    llm = ScriptedLLM(['{"steps":[{"id":"1","action":"search","params":{}}]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    async with gateway_run():
        await planner.generate("查点东西", ["search"], skills="[SKILLS]\n### Skill: 写周报\n模板")
    prompt = "".join(m.content for m in llm.calls[0])
    assert "可参考的技能指引" in prompt
    assert "写周报" in prompt


async def test_replan_injects_skills() -> None:
    """skill 全文随 replan 一起送达——失败步骤的替换步骤也遵循 skill 指引。"""
    llm = ScriptedLLM(['{"steps":[{"id":"2r","action":"transfer","params":{}}]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    plan = Plan(steps=(PlanStep(step_id="2", action="transfer", params={}),))
    failed = plan.get_step("2")
    assert failed is not None
    async with gateway_run():
        await planner.replan(
            plan, failed, "boom", ["transfer"], skills="[SKILLS]\n### Skill: 写周报\n模板"
        )
    prompt = "".join(m.content for m in llm.calls[0])
    assert "可参考的技能指引" in prompt
    assert "写周报" in prompt
