"""
agent 节点 bind_tools 调模型，tools 节点用 ToolNode 执行，review 节点核对
回答 vs 轨迹。review 是必选闸门：回答必须过自检（核对退出条件）才能结束，
没有「无审查的纯工具回环」退化路径。

工具性质（只读/写、来源评级、是否强制幂等）来自 ``ToolRegistry``（拦截契约），
不再散落在多份硬编码字典里：tools 节点据此做写工具 HITL 门禁、幂等键注入、红绿灯
错误格式化；工具结果据此做零信任来源评级。

模块级纯函数（``format_tool_error``/``inject_idempotency_keys``/``evaluate_tool_results``）
见 ``reactive_helpers.py``。
"""

import logging
from collections.abc import Collection
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import ToolNode

from app.agent.gateway import LLMGateway, run_budget
from app.agent.guardrail.injection import scan_tool_calls
from app.agent.guardrail.review import (
    REVIEW_NODE,
    OutputReviewer,
    SideEffectVerifier,
)
from app.agent.guardrail.review_node import build_review_node, route_after_review
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.error_classifier import classify_error
from app.agent.resilience.security_breaker import SecurityBreaker, SecurityBreakerTripped
from app.agent.runtime.budget import BudgetTracker
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.context import (
    CompressionLevel,
    ContextBudget,
    ContextManager,
    Layer,
    RunState,
    format_last_error,
    format_state,
)
from app.agent.runtime.loop_guard import fingerprint
from app.agent.runtime.reactive_helpers import (
    blocked_permanent_calls,
    collect_trace,
    evaluate_tool_results,
    failed_tool_call,
    format_tool_error,
    inject_idempotency_keys,
    resolve_approvals,
)
from app.agent.runtime.state import AgentState
from app.agent.toolmeta import ToolRegistry, validate_param_contract
from app.chunking.base import estimate_tokens

logger = logging.getLogger(__name__)


def _state_block(state: AgentState) -> str:
    """L1 状态快照（含 ``[STATE]`` 带内标记），agent 装配与 compress 计 token 共用。"""
    return format_state(
        turn_count=state.get("turn_count", 0),
        tool_failures=state.get("tool_failures", 0),
        last_action=state.get("last_action", ""),
        state=state.get("run_state") or RunState.RUNNING.value,
        last_error=format_last_error(state.get("last_error")),
    )


def _merge_adjacent_user(messages: list[BaseMessage]) -> list[BaseMessage]:
    """合并相邻 ``HumanMessage``，满足「user/assistant 严格交替」的最严厂商约束。

    连续 user 的两类来源：① 压缩摘要（`[HISTORY SUMMARY]`/`[TOPIC SUMMARY]`）前置后，历史头
    若同为 user；② 本节点把 L2/L3/L1/L4 固定层拼到历史尾 user 之后。合并用空行连接，各块
    带内标记保留、信息不丢；只动 HumanMessage，assistant/tool 不动（tool 配对已由压缩层保证）。
    """
    merged: list[BaseMessage] = []
    for msg in messages:
        if merged and isinstance(msg, HumanMessage) and isinstance(merged[-1], HumanMessage):
            prev = merged[-1]
            merged[-1] = HumanMessage(content=f"{prev.content}\n\n{msg.content}")
        else:
            merged.append(msg)
    return merged


def _fit_skill_entries(invoked_skills: dict[str, str], skills_budget: int) -> list[str]:
    """按 token 预算贪心保留完整 skill 条目（先到先得，塞不下的整条丢弃）。"""
    remaining = skills_budget
    kept: list[str] = []
    for name, content in invoked_skills.items():
        entry = f"### Skill: {name}\n\n{content}"
        cost = estimate_tokens(entry)
        if cost > remaining and kept:
            break
        kept.append(entry)
        remaining -= cost
    return kept


def _build_invoked_skills_block(invoked_skills: dict[str, str], skills_budget: int) -> str:
    """L3 ``[INVOKED SKILLS]`` 块：本会话已加载的自定义 skill 全文（跨轮次持久）。

    按 token 预算截断（对齐 L2 裁剪的贪心策略）：逐条估算，塞不下的整条丢弃——L3 独立
    25k 预算（文档 §2.1「超 25k 截断」），截断防 skills 全文膨胀挤压 L5 余量。
    """
    if not invoked_skills:
        return ""
    kept = _fit_skill_entries(invoked_skills, skills_budget)
    if not kept:
        return ""
    body = "\n\n---\n\n".join(kept)
    return "[INVOKED SKILLS]\n以下 skills 已在本会话加载，请持续遵守其指引：\n\n" + body


def _fixed_tokens(state: AgentState, invoked_skills: dict[str, str], skills_budget: int) -> int:
    """L0+L1+L2+L3+L4 固定层 token 之和（ratio 分母的一部分，L5 之外的部分）。

    L3 只计**截断后**的 skills token（与 ``_build_invoked_skills_block`` 一致），
    保证 ratio 与真实装配占用吻合，不因全量 skills 而虚高。
    """
    skills_tokens = sum(
        estimate_tokens(e) for e in _fit_skill_entries(invoked_skills, skills_budget)
    )
    return (
        estimate_tokens(state.get("system_prompt", ""))
        + estimate_tokens(state.get("memory_block", ""))
        + estimate_tokens(state.get("reminder", ""))
        + estimate_tokens(_state_block(state))
        + skills_tokens
    )


def build_reactive_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    reviewer: OutputReviewer,
    checkpointer: Any,
    runtime: RuntimeConfig,
    verifier: SideEffectVerifier,
    registry: ToolRegistry,
    breaker: CircuitBreaker,
    security_breaker: SecurityBreaker,
    gateway: LLMGateway,
    tracker: BudgetTracker,
    review_max_attempts: int = 2,
    context_manager: ContextManager | None = None,
    invoked_skills: dict[str, str] | None = None,
    count_turn: bool = True,
    tool_names: Collection[str] | None = None,
) -> Any:
    """构建 reactive 图：agent ⇄ tools ⇄ review 自检 + 安全闸（RuntimeConfig）。

    `reviewer` 是必选参数：回答必须过 review 节点核对「最终回答 vs 工具轨迹」才能结束，
    循环不会因为模型停止调用工具就退出——先检查退出条件（审查 verdict 非 mismatch）。

    `runtime` 打包四轴预算 / 防循环 / 注入闸 / HITL / 全局日预算五类安全旋钮（恒在场）。
    `registry` 是「工具名 → ToolMeta」的单一真源，供写工具门禁 / 幂等键注入 /
    来源评级 / 错误格式化消费。写工具分级审批（`approval_policy`）命中
    REQUIRE_APPROVAL 时在 tools 节点 `interrupt()` 挂起，需 checkpointer 持久化，
    前端确认后 `Command(resume=...)` 重放。
    `tracker` 由 service 层创建并传入（必填）——service 需要它在 run 结束时导出
    账本快照（done 事件落单位成本），故把创建权上移，图内仍以闭包消费同一对象。
    """
    invoked_skills = invoked_skills if invoked_skills is not None else {}
    rt = runtime
    # 六层预算的单一权威来源（L0/L1 告警、L2 裁剪、L3 截断共用），闭包下传 agent/compress 节点。
    budget = ContextBudget(rt.context)
    l3_budget = budget.layer_budget(Layer.L3) or 0  # L3 恒有预算，`or 0` 仅作类型兜底
    # 工具白名单（QA 只读集 / 子 Agent 只读五件套）：None = 全量。过滤 tools 与 registry，
    # 使 write_tool_names / bindable_tools 都基于过滤后的子集。
    if tool_names is not None:
        tools = [t for t in tools if t.name in tool_names]
        registry = {name: meta for name, meta in registry.items() if name in tool_names}
    # 注意力稀释铁律 #5：熔断工具从候选集剔除——模型根本看不到它，而不是等模型调用后
    # 再返回「暂时不可用」字符串（那样 schema 仍占 token、模型仍空转 tool_call token）。
    # 级联：resource 熔断的工具同样剔除（DB 挂 → 所有 db 工具一起从候选集消失）。
    resources = {name: meta.resource for name, meta in registry.items()}
    available = set(breaker.available((t.name for t in tools), resources))
    bindable_tools = [t for t in tools if t.name in available]
    bound_model = cast(BaseChatModel, model.bind_tools(bindable_tools))
    write_tool_names = frozenset(name for name, meta in registry.items() if meta.has_side_effect)

    async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        rt.loop_guard.check_context(state["messages"])
        # 六层装配：system(L0) → history(L5) → [memory(L2)+skills(L3)+state(L1)+reminder(L4)]。
        # 固定层（L0/L2/L1/L4）每轮临时拼进输入、不进 state/checkpoint——保前缀稳定 +
        # 压缩只打 L5（state["messages"]）。L2/L3/L1/L4 各带 [MEMORY]/[INVOKED SKILLS]/[STATE]/
        # [REMINDER] 带内标记，合成一条 HumanMessage——否则连续 user 消息会触发 DeepSeek 等
        # 严格 provider 的「user/assistant 必须交替」400。
        messages: list[BaseMessage] = []
        system_prompt = state.get("system_prompt", "")
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.extend(state["messages"])
        context_blocks: list[str] = []
        memory_block = state.get("memory_block", "")
        if memory_block:
            context_blocks.append(memory_block)
        skills_block = _build_invoked_skills_block(invoked_skills, l3_budget)
        if skills_block:
            context_blocks.append(skills_block)
        state_block = _state_block(state)
        state_tokens = estimate_tokens(state_block)
        if budget.is_over(Layer.L1, state_tokens):
            # L1 超 15% 仅告警（文档 §2.1）：状态快照是轻量标量，超限罕见但应可见，不裁剪。
            logger.warning(
                "L1 状态快照超预算：%d/%d tokens（仅告警，不裁剪）",
                state_tokens,
                budget.layer_budget(Layer.L1),
            )
        context_blocks.append(state_block)
        reminder = state.get("reminder", "")
        if reminder:
            context_blocks.append(reminder)
        if context_blocks:
            messages.append(HumanMessage(content="\n\n".join(context_blocks)))
        # 严格交替（Anthropic/DeepSeek 最严约束）：合并相邻 user，覆盖「历史尾 user + 固定层」
        # 与「压缩摘要 + 历史头 user」两类连续 user；各块带内标记保留，assistant/tool 不动。
        messages = _merge_adjacent_user(messages)
        # 统一出口：预算门禁 + 80% 软提示 + 调用级快照 + 重试/超时 + 记账（决策 D1/D6/D7）
        configurable = config.get("configurable") or {}
        run_id = str(configurable.get("thread_id", ""))
        with run_budget(tracker, run_id=run_id):
            response = await gateway.invoke_model(
                "agent", bound_model, messages, count_turn=count_turn
            )
        update: dict[str, Any] = {
            "messages": [response],
            "turn_count": state.get("turn_count", 0) + 1,
            "final_answer": str(response.content),
        }
        return update

    # 工具失败追踪（第五层状态「崩溃现场」）：ToolNode 用 handle_tool_errors 把异常格式化成
    # 给模型的文本，异常本身不向上抛。这里包一层，在格式化同时把「分类标签」记进闭包 holder，
    # 供 tool_node 落 last_error（随 checkpoint 持久化，崩溃恢复后据此判断是否重试）。
    error_holder: dict[str, str] = {}

    def _format_tool_error_tracked(exc: Exception) -> str:
        error_holder["message"] = str(exc)
        error_holder["kind"] = classify_error(exc)
        return format_tool_error(exc)

    raw_tool_node = ToolNode(bindable_tools, handle_tool_errors=_format_tool_error_tracked)

    def _enforce_param_contracts(tool_calls: list[Any]) -> list[ToolMessage] | None:
        """防线②：执行前按 ToolMeta.param_contract 统一校验，违规 fail-closed。

        返回违规消息列表（非 None 即整轮拒收、不再执行）；反复违规喂安全熔断，
        达阈值即冻结（异常上抛硬停）。
        """
        if not tool_calls:
            return None
        violation_messages: list[ToolMessage] = []
        had_violation = False
        for tc in tool_calls:
            name = tc.get("name", "")
            meta = registry.get(name)
            violation = validate_param_contract(
                name,
                tc.get("args"),
                meta.param_contract if meta is not None else None,
            )
            if violation is None:
                violation_messages.append(
                    ToolMessage(
                        content="（本轮存在参数非法调用，本调用未执行，请修正后重试）",
                        tool_call_id=tc.get("id") or "",
                    )
                )
                continue
            had_violation = True
            violation_messages.append(
                ToolMessage(content=violation, tool_call_id=tc.get("id") or "")
            )
            if security_breaker.record_violation():
                raise SecurityBreakerTripped(
                    f"安全熔断已触发：反复参数校验失败（最新：{violation}）"
                )
        return violation_messages if had_violation else None

    async def tool_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        configurable = config.get("configurable") or {}
        run_id = str(configurable.get("thread_id", ""))
        last = state["messages"][-1]
        tool_calls = last.tool_calls if isinstance(last, AIMessage) else []

        # 防线④ 安全熔断：已熔断则冻结本次 run，拒绝执行任何工具（异常上抛硬停）。
        if security_breaker.is_tripped():
            raise SecurityBreakerTripped("安全熔断已触发：Agent 行为疑似越界，已冻结本次 run")

        # 防线② 工具调用量上限：执行前计数 + 预检第五轴（穷举爆破的最后一道闸）。
        if tool_calls:
            tracker.record_tool_calls(len(tool_calls))

        # 防线② 参数契约校验：违规 fail-closed（返回消息给模型改，或熔断冻结）。
        if (violation_messages := _enforce_param_contracts(tool_calls)) is not None:
            return {"messages": violation_messages}

        # 防线③ 写工具分级审批：高风险同步审批挂起等人裁决，拒绝则返回消息。
        if isinstance(last, AIMessage) and last.tool_calls:
            if (denied := resolve_approvals(last.tool_calls, registry, rt)) is not None:
                return {"messages": denied}

        # 执行契约：给强制幂等的写工具注入「业务意图」幂等键（内容派生，跨重试复用同键）
        state = inject_idempotency_keys(state, run_id, registry)
        # 防线：崩溃恢复/防死循环——last_error 永久失败的同工具+同参数不再重放
        # （transient 放行重试）。在幂等键注入后比较 fingerprint，与 last_error 写入时的
        # fingerprint 同源（写工具注入的 idempotency_key 内容派生、跨重试稳定，注入前比较
        # 会因缺少该键而判不匹配）。
        injected_calls = (
            state["messages"][-1].tool_calls
            if isinstance(state["messages"][-1], AIMessage)
            else tool_calls
        )
        if blocked := blocked_permanent_calls(state, injected_calls):
            return {"messages": blocked}
        # 工具执行可能调网关（如 search_knowledge_base 走 retriever 的 embed/rerank），
        # 必须带 run context 才能记账/快照（不可旁路）。
        with run_budget(tracker, run_id=run_id):
            result = await raw_tool_node.ainvoke(state)
        # 必须在 evaluate 之前反查失败工具：sanitize 会改 content，且失败文本特征据此识别。
        failed_tc = failed_tool_call(state, result)
        result = evaluate_tool_results(state, result, registry)

        update: dict[str, Any] = {"messages": result["messages"]}
        # 统一轨迹收集：review 不再事后从 messages 还原，改为执行时收集（reactive/planner 共用）
        trace = collect_trace(tool_calls, result)
        if trace:
            update["trace"] = trace
        # 本轮若有工具失败，把分类后的崩溃现场写进 last_error（sticky：无失败不覆盖，保留上一次）
        if error_holder:
            failed_name = failed_tc.get("name", "") if failed_tc else ""
            update["last_error"] = {
                "message": error_holder["message"],
                "kind": error_holder["kind"],
                "tool": failed_name,
                "fingerprint": fingerprint(failed_name, failed_tc.get("args")) if failed_tc else "",
            }
            update["tool_failures"] = state.get("tool_failures", 0) + 1
            error_holder.clear()
        # L1 快照回写：最后一次动作（本轮工具名）。
        if isinstance(last, AIMessage) and last.tool_calls:
            update["last_action"] = last.tool_calls[-1].get("name", "")
        return update

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            rt.loop_guard.check_tool_calls(last.tool_calls)
            scan_tool_calls(last.tool_calls, rt.injection_policy)
            return "tools"
        # 模型不再调用工具 ≠ 可以结束：必须过 review 核对退出条件（回答 vs 轨迹一致）才能停。
        return REVIEW_NODE

    async def compress_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """run 内上下文压缩节点：ratio 超阈值时按级别压缩 state["messages"]（整体替换）。

        用 ``RemoveMessage(id=REMOVE_ALL_MESSAGES)`` 整体替换消息列表，使压缩后的状态
        真正瘦身（checkpoint 也变小），而非每轮在模型输入上做临时复制。命中压缩级别
        写入 ``compression_level``，供 service 层记 observability 事件。
        """
        if context_manager is None:
            return {"compression_level": 0}
        fixed = _fixed_tokens(state, invoked_skills, l3_budget)
        level = context_manager.current_level(state["messages"], fixed)
        if level == CompressionLevel.NONE:
            return {"compression_level": 0}
        # 压缩摘要走网关（summarizer 恒在场），须带 run context 才能记账/快照（不可旁路）。
        configurable = config.get("configurable") or {}
        run_id = str(configurable.get("thread_id", ""))
        with run_budget(tracker, run_id=run_id):
            compressed = await context_manager.compress(
                state["messages"], level, context_manager.history_budget(fixed)
            )
        logger.info("run 内上下文压缩触发（level=%s）", int(level))
        return {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compressed],
            "compression_level": int(level),
        }

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

    # 压缩节点钉在 agent 之前：每次进 agent（首轮 / 工具后 / 审查重试后）都先过一遍，
    # 保证工具结果累积时上下文始终有被压缩的机会，而不是只有首轮过一次。
    agent_entry = "compress" if context_manager is not None else "agent"
    if context_manager is not None:
        graph.add_node("compress", compress_node)
        graph.add_edge(START, "compress")
        graph.add_edge("compress", "agent")
    else:
        graph.add_edge(START, "agent")

    graph.add_node(
        REVIEW_NODE,
        build_review_node(reviewer, review_max_attempts, verifier, tracker, write_tool_names),
    )
    graph.add_conditional_edges(
        "agent", route_after_agent, {"tools": "tools", REVIEW_NODE: REVIEW_NODE}
    )
    graph.add_conditional_edges(REVIEW_NODE, route_after_review, {"agent": agent_entry, END: END})
    graph.add_edge("tools", agent_entry)

    return graph.compile(checkpointer=checkpointer)


# =============================================================================
# reactive 图拓扑（所有节点与边）
#
#                    ┌───────┐
#                    │ START │
#                    └───┬───┘
#                        │
#      ┌─────────────────▼───────────────────┐
#  ┌──►│       [compress] ──► [agent]        │
#  │   └─────────────────┬───────────────────┘
#  │                     │
#  │              ┌──────┴──────┐
#  │              │             │
#  │              │             │
#  │              ▼             ▼
#  │          ┌─────────┐   ┌─────────┐
#  │          │  tools  │   │ review  │
#  │          └────┬────┘   └────┬────┘
#  │              │             │
#  │              │           ┌───────────────────┐
#  │              │           │                   │
#  │              │             ▼ mismatch     ▼
#  │              │             │              │
#  │              │             │           ┌─────┐
#  │              │             │           │ END │
#  │              │             │           └─────┘
#  │              └──────┬──────┘
#  │                     │
#  └─────────────────────┘
#
#   route_after_agent：有 tool_calls → tools；无 tool_calls → review（必过，无纯工具回环）。
#   route_after_review：mismatch → 回到入口框继续修复（attempts 累加）；
#   ok / repaired / unverified / corrected → END。
#   有压缩器时每轮进 agent 前先过 compress，保证工具结果累积时上下文始终有机会被压缩。
# =============================================================================
