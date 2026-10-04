"""输出审查（guardrail）的图内节点层：review 节点 + 路由。

内容层（reviewer 协议 / LLM 实现 / 判定契约）见 `review.py`。本模块是节点层，负责：
- 从统一轨迹（`state["trace"]`，执行时由 `collect_trace` 收集）还原「最终回答 vs 工具轨迹」、
  提取写工具副作用（`write_side_effects_from_trace`）供确定性对账；
- `build_review_node` 产出图内 review 节点——先跑确定性 `verifier` 回查副作用，
  再跑 `reviewer` 的 LLM 判定，不一致则注入纠正指令回环（有界），超限则诚实更正；
- `route_after_review` 决定 mismatch 回 agent 继续修复，否则结束。
"""

import json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END

from app.agent.gateway import run_budget
from app.agent.guardrail.review import (
    OutputReviewer,
    ReviewIssue,
    ReviewResult,
    ReviewVerdict,
    SideEffectVerifier,
)
from app.agent.runtime.budget import BudgetTracker
from app.agent.runtime.context import RunState
from app.agent.runtime.state import AgentState

_TRACE_RESULT_CHARS = 2000


def format_trace(trace: list[dict[str, Any]]) -> str:
    """把统一轨迹序列化为「工具轨迹」文本，供审查器对账。

    trace 由 ``collect_trace`` 在执行时收集（不再从 messages 事后还原），本函数只负责
    序列化：result 超长截断到 ``_TRACE_RESULT_CHARS``，无返回补「（未返回）」。
    """
    if not trace:
        return "（本轮未调用任何工具）"
    lines = ["工具轨迹："]
    for t in trace:
        content = str(t.get("result", ""))
        if len(content) > _TRACE_RESULT_CHARS:
            content = content[:_TRACE_RESULT_CHARS] + "…"
        text = f"- {t['tool']}({json.dumps(t.get('args') or {}, ensure_ascii=False)})"
        text += f" → {content}" if content else " → （未返回）"
        lines.append(text)
    return "\n".join(lines)


def write_side_effects_from_trace(
    trace: list[dict[str, Any]],
    write_tool_names: frozenset[str],
) -> list[tuple[str, dict[str, Any], str]]:
    """从统一轨迹提取写工具的 (工具名, 参数, 返回内容)，供确定性副作用对账。"""
    return [
        (t["tool"], t.get("args") or {}, str(t.get("result", "")))
        for t in trace
        if t["tool"] in write_tool_names
    ]


def _issues_to_dicts(issues: list[ReviewIssue]) -> list[dict[str, str]]:
    return [{"claim": i.claim, "tool": i.tool, "evidence": i.evidence} for i in issues]


def _build_repair_note(review: ReviewResult, write_tool_names: frozenset[str]) -> str:
    """据审查结果生成纠正指令（优先针对写工具漏做/失败/副作用未落库）。"""
    write_issues = [i for i in review.issues if i.tool in write_tool_names]
    if write_issues:
        issue = write_issues[0]
        reason = {
            "no_tool_call": "并未调用",
            "tool_failed": "调用返回失败",
            "side_effect_missing": "调用声称成功但副作用未真正落库",
        }.get(issue.evidence, "未成功完成")
        return (
            f"（系统自检）你上一条回答声称「{issue.claim}」，但实际工具{issue.tool}{reason} 。"
            f"请现在立即真的调用 {issue.tool} 完成该写操作，然后向用户如实汇报结果；"
            "若确实无法完成，就诚实说明没做到，不要假装已完成。"
        )
    return (
        "（系统自检）你上一条回答声称完成了一项操作，但工具轨迹显示并未实际执行。"
        "请补做，或向用户如实说明。"
    )


def _honest_correction(review: ReviewResult) -> str:
    """修复失败后的诚实更正兜底：追加一段说明，不误导用户。"""
    claims = "、".join(i.claim for i in review.issues) or "上述操作"
    return (
        f"\n\n> ⚠️ 自检更正：我上面说已完成「{claims}」，但实际上没有成功执行。"
        "抱歉，请让我重新处理，或稍后重试。"
    )


def _unverified_note() -> str:
    """审查器失能（LLM 异常/解析失败）时的诚实提示，fail-closed 地暴露不确定性。"""
    return "\n\n> ⚠️ 自检未完成：本次回答未能完成完整性审查，请以实际执行结果为准。"


def build_review_node(
    reviewer: OutputReviewer,
    review_max_attempts: int,
    verifier: SideEffectVerifier,
    tracker: BudgetTracker,
    write_tool_names: frozenset[str] = frozenset(),
) -> Any:
    """产出图内 review 节点：确定性副作用对账 + LLM 对账，返回 verdict/issues/correction/attempts。

    顺序：先跑确定性 `verifier` 回查写工具副作用（命中失败即强制 mismatch，不依赖 LLM），
    再跑 `reviewer` 的 LLM 判定；`reviewer` 失能（UNVERIFIED）则 fail-closed 追加诚实更正。
    `tracker` 是本 run 四轴预算（可选）：reviewer 走网关时，用 ``run_budget`` 把
    tracker + thread_id 注入 contextvar，使网关命中本 run 的预算门禁与调用级快照。
    """

    async def review_node(
        state: AgentState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        trace = state.get("trace", [])
        final_answer = state.get("final_answer", "")
        result: ReviewResult | None = None
        for name, args, tool_result in write_side_effects_from_trace(trace, write_tool_names):
            reason = await verifier.verify(name, args, tool_result)
            if reason is not None:
                result = ReviewResult(
                    verdict=ReviewVerdict.MISMATCH,
                    issues=[ReviewIssue(claim=reason, tool=name, evidence="side_effect_missing")],
                )
                break
        if result is None:
            assert config is not None, (
                "review 节点必须在 config 中携带 thread_id（=run_id）；"
                "run_id 是快照/记账的划界键，缺它不可静默跳过"
            )
            run_id = str(config.get("configurable", {}).get("thread_id", ""))
            with run_budget(tracker, run_id=run_id):
                result = await reviewer.review(final_answer, format_trace(trace))

        attempts = state.get("attempts", 0)
        if result.verdict is ReviewVerdict.UNVERIFIED:
            # 审查器失能（LLM 异常/解析失败）：无判定可更正，fail-closed 暴露不确定性
            # correction 写入自检未完成提示，由 service 层拼进最终回答
            return {
                "review_verdict": "unverified",
                "review_issues": [
                    {"claim": "审查器未能完成对账", "tool": "", "evidence": "review_unavailable"}
                ],
                "correction": _unverified_note(),
                "run_state": RunState.COMPLETED.value,
            }
        if result.verdict is ReviewVerdict.OK:
            # 审查通过：修过一轮则为 repaired，首轮通过则为 ok
            verdict = "repaired" if attempts > 0 else "ok"
            return {
                "review_verdict": verdict,
                "review_issues": [],
                "run_state": RunState.COMPLETED.value,
            }
        issues = _issues_to_dicts(result.issues)
        if attempts >= review_max_attempts:
            # 不一致且已修满上限：放弃重试，追加诚实更正兜底
            # correction 写入自检更正提示，由 service 层拼进最终回答
            return {
                "review_verdict": "corrected",
                "review_issues": issues,
                "correction": _honest_correction(result),
                "run_state": RunState.COMPLETED.value,
            }
        # 不一致且未到上限：注入纠正指令回 agent 继续修复（attempts 累加）
        return {
            "review_verdict": "mismatch",
            "review_issues": issues,
            "attempts": attempts + 1,
            "messages": [HumanMessage(content=_build_repair_note(result, write_tool_names))],
        }

    return review_node


def route_after_review(state: AgentState) -> str:
    """review 后路由：mismatch 回 agent 继续修复，否则结束。"""
    return "agent" if state.get("review_verdict") == "mismatch" else END
