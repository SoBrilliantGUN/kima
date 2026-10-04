"""ChainOptimizer：关键路径（最长串行链）+ verdict 三档。"""

from app.agent.runtime.chain_optimizer import analyse_plan
from app.agent.runtime.planner import Plan, PlanStep


def test_critical_path_serial() -> None:
    """3 步串行链 → 关键路径 3，短链 ok。"""
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="a"),
            PlanStep(step_id="2", action="b", depends_on=("1",)),
            PlanStep(step_id="3", action="c", depends_on=("2",)),
        )
    )
    report = analyse_plan(plan, {})
    assert report.critical_path == 3
    assert report.verdict == "ok"


def test_critical_path_parallel_branch() -> None:
    """并行分支不拖长关键路径：2/3 依赖 1 且互不依赖 → 关键路径 2。"""
    plan = Plan(
        steps=(
            PlanStep(step_id="1", action="a"),
            PlanStep(step_id="2", action="b", depends_on=("1",)),
            PlanStep(step_id="3", action="c", depends_on=("1",)),
        )
    )
    report = analyse_plan(plan, {})
    assert report.critical_path == 2


def test_critical_path_long_chain_is_critical() -> None:
    """19 步串行链 → 关键路径 19，verdict critical（极长链提示）。"""
    steps = [PlanStep(step_id="1", action="a")]
    for i in range(2, 20):
        steps.append(PlanStep(step_id=str(i), action="a", depends_on=(str(i - 1),)))
    report = analyse_plan(Plan(steps=tuple(steps)), {})
    assert report.critical_path == 19
    assert report.verdict == "critical"
    assert report.issues


def test_13_step_chain_not_critical() -> None:
    """13 步串行链不再 critical——长参数链是合法形态，不因串行链长被硬砍。"""
    steps = [PlanStep(step_id="1", action="a")]
    for i in range(2, 14):
        steps.append(PlanStep(step_id=str(i), action="a", depends_on=(str(i - 1),)))
    report = analyse_plan(Plan(steps=tuple(steps)), {})
    assert report.critical_path == 13
    assert report.verdict != "critical"
