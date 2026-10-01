"""reactive 执行循环的模块级纯函数（图节点之外的 helper，供 ``build_reactive_graph`` 调用）。"""

import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.guardrail.trust import is_red_line, sanitize_content
from app.agent.helpers import tool_source
from app.agent.resilience.result import ToolFailure
from app.agent.runtime.loop_guard import fingerprint
from app.agent.runtime.state import AgentState
from app.agent.toolmeta import ToolRegistry, idempotency_key_for

logger = logging.getLogger(__name__)


def format_tool_error(exc: Exception) -> str:
    """把工具异常格式化为给模型看的红绿灯文本（反馈契约）。"""
    if isinstance(exc, ToolFailure):
        return exc.to_message()
    return f"Error: {exc}"


def inject_idempotency_keys(
    state: AgentState, run_id: str, registry: ToolRegistry
) -> AgentState:
    """给本轮写工具调用注入「业务意图」幂等键（执行契约）。

    键 = ``{run_id}:{tool_name}:sha256(规范化 key_fields)``，``key_fields`` 来自
    ``ToolMeta.idempotency_key_fields``——同一业务意图跨重试/崩溃/回环重发拿到同一个键
    （而非「第几次调用」的位置序号）；不同参数则键自然不同。同键不同参数的冲突检测由
    工具执行层的 request_hash 配合幂等表（``repositories/idempotency.py``）完成。
    """
    messages = list(state["messages"])
    if not messages:
        return state
    last = messages[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return state
    new_calls: list[Any] = []
    for tc in last.tool_calls:
        meta = registry.get(tc.get("name", ""))
        if meta is not None and meta.enforced_idempotent and meta.idempotency_key_fields:
            args = dict(tc.get("args") or {})
            args["idempotency_key"] = idempotency_key_for(
                meta.name, args, meta.idempotency_key_fields, run_id
            )
            new_calls.append({**tc, "args": args})
        else:
            new_calls.append(tc)
    messages[-1] = last.model_copy(update={"tool_calls": new_calls})
    return {**state, "messages": messages}


def _tool_call_by_id(state: AgentState) -> dict[str, Any]:
    """从 state 的 AIMessage tool_calls 构建 ``tool_call_id → tool_call`` 映射（含 name/args）。"""
    by_id: dict[str, Any] = {}
    for msg in state["messages"]:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                call_id = tc.get("id")
                if call_id:
                    by_id[call_id] = tc
    return by_id


def _tool_name_by_id(state: AgentState) -> dict[str, str]:
    """从 state 的 AIMessage tool_calls 构建 ``tool_call_id → 工具名`` 映射。"""
    return {cid: tc.get("name", "") for cid, tc in _tool_call_by_id(state).items()}


def failed_tool_call(state: AgentState, result: dict[str, Any]) -> dict[str, Any] | None:
    """返回本轮最后一次工具失败的 tool_call（``{name, args, id}``，无失败返回 None）。

    ToolNode 用 ``handle_tool_errors`` 把异常格式化成失败文本（``format_tool_error`` 产出
    ``Error: ...`` / ``⚠️ ...`` / ``❌ ...``），成功结果不会以这些前缀开头。据此从 raw
    result 反查失败 ToolMessage，再经 ``_tool_call_by_id`` 取完整 tool_call（含 args，供
    幂等拦截算 fingerprint）。必须在 ``evaluate_tool_results`` 之前调用（sanitize 会改 content）。
    """
    by_id = _tool_call_by_id(state)
    failed: dict[str, Any] | None = None
    for msg in result.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        content = str(msg.content)
        if content.startswith("Error:") or content.startswith("⚠️") or content.startswith("❌"):
            failed = by_id.get(msg.tool_call_id)
    return failed


def blocked_permanent_calls(state: AgentState, tool_calls: list[Any]) -> list[ToolMessage]:
    """崩溃恢复/防死循环裁决：读 last_error，permanent 且本轮仅重试「同工具+同参数」→ 拦截。

    返回非空 = 整轮拦截（每个 tool_call 一条「已永久失败」消息）；空 = 放行。transient 不拦
    （放行重试，``with_retry`` 再试）。用 fingerprint（同工具+同参数的稳定哈希，剔除 limit/
    offset 等易变键）精确匹配——换参数重试同名工具是合理行为，不拦。覆盖两条路径：① 崩溃
    恢复重放 tool_node（state 带上次 permanent 失败的 last_error，重放同参数调用时跳过，不再
    空转调用）；② 正常循环模型反复调同一个 permanent 失败操作（早于 LoopGuard 的 5 次阈值止损）。
    """
    last_err = state.get("last_error")
    if not last_err or last_err.get("kind") != "permanent":
        return []
    fp = last_err.get("fingerprint", "")
    if not fp or not tool_calls:
        return []
    if not all(fingerprint(tc.get("name"), tc.get("args")) == fp for tc in tool_calls):
        return []  # 含不同操作（换参数/换工具）→ 不整轮拦截，交给正常执行
    return [
        ToolMessage(content="该操作已永久失败，请勿重复重试。", tool_call_id=tc.get("id") or "")
        for tc in tool_calls
    ]


def evaluate_tool_results(
    state: AgentState, result: dict[str, Any], registry: ToolRegistry
) -> dict[str, Any]:
    """L2 检索节点：对每份工具返回做零信任处置，返回带 <data trust> 标记的消息。

    每份工具返回各自成块、各自带分（红线阻断=占位；隔离=+15；脱敏=打码；放行/观察=原样），
    分数随 ``<data trust=... source=...>`` 标记流动，不再折成一个 run 级标量。
    """
    name_by_id = _tool_name_by_id(state)

    sanitized: list[Any] = []
    for msg in result.get("messages", []):
        if not isinstance(msg, ToolMessage):
            sanitized.append(msg)
            continue
        name = name_by_id.get(msg.tool_call_id, "")
        content = str(msg.content)
        if is_red_line(content):
            logger.warning("L2 红线阻断（tool_result:%s）", name)
        sanitized.append(
            ToolMessage(
                content=sanitize_content(content, tool_source(name, registry)),
                tool_call_id=msg.tool_call_id,
            )
        )
    return {"messages": sanitized}
