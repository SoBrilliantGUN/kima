"""Agent 轨迹评测 harness：golden 加载 + 轨迹断言 + 报告。"""

from pathlib import Path

from app.agent.evaluation.trajectory import (
    Trajectory,
    TrajectoryCase,
    format_report,
    load_trajectory_cases,
    run_trajectory_eval,
)


async def test_trajectory_eval_pass_and_fail() -> None:
    cases = [
        TrajectoryCase(
            question="a", expected_tools=("list_notes",), expected_answer_contains="没有"
        ),
        TrajectoryCase(question="b", expected_tools=("write_memory",)),
    ]

    async def run(question: str) -> Trajectory:
        return {
            "a": Trajectory(tools=("list_notes",), answer="库里没有笔记。"),
            "b": Trajectory(tools=("search_memory",), answer="记下了。"),
        }[question]

    report = await run_trajectory_eval(run, cases)
    assert report.pass_rate == 0.5
    assert report.results[0].passed is True
    assert report.results[1].passed is False
    assert "工具轨迹不符" in report.results[1].reason


async def test_load_trajectory_cases(tmp_path: Path) -> None:
    path = tmp_path / "golden.json"
    path.write_text(
        '[{"question":"q","expected_tools":["list_notes","read_note"],'
        '"expected_answer_contains":"x"}]',
        encoding="utf-8",
    )
    cases = load_trajectory_cases(path)
    assert len(cases) == 1
    assert cases[0].question == "q"
    assert cases[0].expected_tools == ("list_notes", "read_note")
    assert cases[0].expected_answer_contains == "x"


async def test_format_report() -> None:
    async def run(question: str) -> Trajectory:
        return Trajectory(tools=(), answer="ok")

    report = await run_trajectory_eval(run, [TrajectoryCase(question="q")])
    text = format_report(report)
    assert "通过率 100%" in text
    assert "[PASS] q" in text
