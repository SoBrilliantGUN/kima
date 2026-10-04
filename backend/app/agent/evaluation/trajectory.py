"""Agent 轨迹评测：golden 样本 → 跑 agent → 断言工具轨迹 + 最终回答。

与 `app/rag/eval_runner.py`（检索级 recall@k/MRR）互补：这里测的是 Agent 的「行为」——
调了哪些工具、顺序、最终回答是否含关键内容。golden 集用「期望工具名序列 + 回答应含片段」
标注，不绑 UUID，可跨次重跑。P2 起再接 LLM-judge 做答案质量（当前只做轨迹 + 子串断言）。
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

RunFn = Callable[[str], Awaitable["Trajectory"]]


@dataclass(frozen=True)
class Trajectory:
    """一次 agent run 的行为快照：工具名序列 + 拼接后的最终回答。"""

    tools: tuple[str, ...]
    answer: str


@dataclass(frozen=True)
class TrajectoryCase:
    """一条 golden 样本：问题 + 期望工具序列 + 回答应含的片段。"""

    question: str
    expected_tools: tuple[str, ...] = ()
    expected_answer_contains: str = ""


@dataclass(frozen=True)
class TrajectoryResult:
    case: TrajectoryCase
    actual: Trajectory
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class TrajectoryEvalReport:
    results: tuple[TrajectoryResult, ...]
    pass_rate: float


def load_trajectory_cases(path: str | Path) -> list[TrajectoryCase]:
    """从 JSON 加载 golden 集：`[{"question", "expected_tools", "expected_answer_contains"}]`。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        TrajectoryCase(
            question=str(item["question"]),
            expected_tools=tuple(item.get("expected_tools", [])),
            expected_answer_contains=str(item.get("expected_answer_contains", "")),
        )
        for item in raw
    ]


async def run_trajectory_eval(run: RunFn, cases: Sequence[TrajectoryCase]) -> TrajectoryEvalReport:
    """逐条跑样本，断言工具序列 + 回答片段，返回通过率。"""
    results = [await _run_case(run, case) for case in cases]
    n = len(results) or 1
    return TrajectoryEvalReport(
        results=tuple(results),
        pass_rate=sum(r.passed for r in results) / n,
    )


async def _run_case(run: RunFn, case: TrajectoryCase) -> TrajectoryResult:
    actual = await run(case.question)
    if case.expected_tools and actual.tools != case.expected_tools:
        return TrajectoryResult(
            case,
            actual,
            False,
            f"工具轨迹不符：期望 {list(case.expected_tools)}，实际 {list(actual.tools)}",
        )
    if case.expected_answer_contains and case.expected_answer_contains not in actual.answer:
        return TrajectoryResult(
            case, actual, False, f"回答缺失片段：{case.expected_answer_contains!r}"
        )
    return TrajectoryResult(case, actual, True)


def format_report(report: TrajectoryEvalReport) -> str:
    """渲染可读报告（供 CLI 打印）。"""
    lines = [
        f"Agent 轨迹评估报告（{len(report.results)} 条，通过率 {report.pass_rate:.0%}）",
        "逐条：",
    ]
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        tools = ",".join(result.actual.tools) or "（无工具）"
        lines.append(f"  [{mark}] {result.case.question}  →  {tools}")
        if not result.passed:
            lines.append(f"        {result.reason}")
    return "\n".join(lines)
