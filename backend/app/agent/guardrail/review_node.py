"""输出审查（guardrail）的图内节点层：review 节点 + 路由。

内容层（reviewer 协议 / LLM 实现 / 判定契约）见 `review.py`。本模块是节点层，负责：
- 从消息流还原「最终回答 vs 工具轨迹」（`_last_answer` / `trace_from_messages`）、
  提取写工具副作用（`_write_side_effects`）供确定性对账；
- `build_review_node` 产出图内 review 节点——先跑确定性 `verifier` 回查副作用，
  再跑 `reviewer` 的 LLM 判定，不一致则注入纠正指令回环（有界），超限则诚实更正；
- `route_after_review` 决定 mismatch 回 agent 继续修复，否则结束。
"""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
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
from app.agent.runtime.state import AgentState

_TRACE_RESULT_CHARS = 2000


def _last_answer(messages: list[AnyMessage]) -> str:
    """取最后一条非空 assistant 回答文本（review 时的最终回答）。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return str(msg.content)
    return ""


def trace_from_messages(messages: Sequence[BaseMessage]) -> str:
    """从消息流还原工具轨迹（工具调用 + 工具返回），供审查器对账。

    按 ``tool_call_id`` 显式把「调用」与「返回」配成对，而非分成两段列表——审查器的
    任务就是核对「声称的调用 → 返回是否成功」，配对正是它要的证据；同名工具多次调用、
    并行调用、或某次返回缺失时，按位置对齐会错配。配不上任何调用的孤儿返回按其在
    消息流中的位置插入（而非甩到末尾）；无名调用保留为 ``unknown(args)`` 继续按 id 配对。
    """
    # ("call", {...}) | ("orphan", {"content": ...})，按消息流顺序，保证孤儿保序
    events: list[tuple[str, dict[str, Any]]] = []
    call_by_id: dict[str, dict[str, Any]] = {}
    result_by_id: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                call_id = tc.get("id")
                call = {
                    "id": call_id,
                    "name": tc.get("name") or "unknown",
                    "args": tc.get("args") or {},
                }
                events.append(("call", call))
                if call_id:
                    call_by_id[call_id] = call
        elif isinstance(msg, ToolMessage):
            content = str(msg.content)
            if len(content) > _TRACE_RESULT_CHARS:
                content = content[:_TRACE_RESULT_CHARS] + "…"
            if msg.tool_call_id and msg.tool_call_id in call_by_id:
                result_by_id[msg.tool_call_id] = content
            else:
                events.append(("orphan", {"content": content}))
    if not events:
        return "（本轮未调用任何工具）"
    lines = ["工具轨迹："]
    for kind, payload in events:
        if kind == "call":
            text = f"- {payload['name']}({json.dumps(payload['args'], ensure_ascii=False)})"
            if payload["id"] in result_by_id:
                text += f" → {result_by_id.pop(payload['id'])}"
            else:
                text += " → （未返回）"
        else:
            text = f"- unknown → {payload['content']}"
        lines.append(text)
    return "\n".join(lines)


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


def _write_side_effects(
    messages: list[AnyMessage],
    write_tool_names: frozenset[str],
) -> list[tuple[str, dict[str, Any], str]]:
    """从消息流提取写工具的 (工具名, 参数, 返回内容)，供确定性副作用对账。"""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    effects: list[tuple[str, dict[str, Any], str]] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                name = tc.get("name", "")
                call_id = tc.get("id")
                if call_id and name in write_tool_names:
                    calls[call_id] = (name, tc.get("args") or {})
        elif isinstance(msg, ToolMessage):
            if msg.tool_call_id in calls:
                name, args = calls[msg.tool_call_id]
                effects.append((name, args, str(msg.content)))
    return effects


def build_review_node(
    reviewer: OutputReviewer,
    review_max_attempts: int,
    verifier: SideEffectVerifier | None = None,
    write_tool_names: frozenset[str] = frozenset(),
    tracker: Any = None,
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
        messages = state["messages"]
        result: ReviewResult | None = None
        if verifier is not None:
            for name, args, tool_result in _write_side_effects(messages, write_tool_names):
                reason = await verifier.verify(name, args, tool_result)
                if reason is not None:
                    result = ReviewResult(
                        verdict=ReviewVerdict.MISMATCH,
                        issues=[
                            ReviewIssue(claim=reason, tool=name, evidence="side_effect_missing")
                        ],
                    )
                    break
        if result is None:
            run_id = None
            if config is not None:
                run_id = str(config.get("configurable", {}).get("thread_id", "")) or None
            with run_budget(tracker, run_id=run_id):
                result = await reviewer.review(
                    _last_answer(messages), trace_from_messages(messages)
                )

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
            }
        if result.verdict is ReviewVerdict.OK:
            # 审查通过：修过一轮则为 repaired，首轮通过则为 ok
            verdict = "repaired" if attempts > 0 else "ok"
            return {"review_verdict": verdict, "review_issues": []}
        issues = _issues_to_dicts(result.issues)
        if attempts >= review_max_attempts:
            # 不一致且已修满上限：放弃重试，追加诚实更正兜底
            # correction 写入自检更正提示，由 service 层拼进最终回答
            return {
                "review_verdict": "corrected",
                "review_issues": issues,
                "correction": _honest_correction(result),
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
