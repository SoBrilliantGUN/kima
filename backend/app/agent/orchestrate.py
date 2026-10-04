"""Copilot 对话运行时的共用编排纯函数。

把原 `CopilotService` / `ResumeMixin` / `PlanModeMixin` 里被多方复用的方法提成模块级
纯函数：`run` / `resume` / `resume_after_crash` 三条入口 + `PlanRunner` 都依赖这一组
收尾与上下文组装，避免再靠 mixin 的 `NotImplementedError` 反向声明来共享。

每个函数第一参数是 `CopilotRuntime`（见 `compose.py`），不再有 `self`。
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.compose import CopilotRuntime
from app.agent.events import (
    CopilotApprovalEvent,
    CopilotDeltaEvent,
    CopilotDoneEvent,
    CopilotReviewEvent,
    CopilotStepEvent,
    CopilotStreamEvent,
)
from app.agent.gateway import run_budget
from app.agent.guardrail.review import REVIEW_NODE, REVIEW_TOOL_NAME
from app.agent.guardrail.sensitive import redact_sensitive, scrub_payload
from app.agent.guardrail.trust import (
    Disposition,
    composite,
    content_trust,
    disposition,
    is_red_line,
    sanitize_content,
    source_trust,
)
from app.agent.helpers import TOOL_RESULT_CHARS, clip
from app.agent.memory import (
    assemble_system_prompt,
    format_memory_block,
    format_reminder,
    format_subagent_constraints,
)
from app.agent.runtime.budget import BudgetTracker, RunAccounting
from app.agent.runtime.context import ContextBudget, Layer, RunState
from app.agent.session import RunSession
from app.chunking.base import estimate_tokens
from app.core.exceptions import NotFoundError
from app.core.skill_store import CustomSkill
from app.models.chat import ChatConversation, ChatKind, ChatMessage, ChatRole
from app.models.copilot import ApprovalStatus, CopilotApproval, CopilotEvent
from app.schemas.copilot import CopilotRequest

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 50
# 缓存命中率告警的输入量下限（token）：小 prompt 首次调用 cache_read 恒为 0 属正常，
# 只有输入量足够大却仍无命中时，才判定「前缀缓存被破坏」（有人往系统提示塞了动态内容）。
_CACHE_HIT_MIN_INPUT_TOKENS = 2000

# —— skill 语义召回（planner 规划期预选相关技能，见 assemble_context）——
# 单次召回最多带回几个 skill（skill 是「怎么做」的引导，宜精不宜多，且全文要进 planner prompt）。
SKILL_RECALL_TOP_K = 3
# 相似度下限：低于此值视为「与本任务无关」，不召回（避免无关 skill 污染 planner 的拆解）。
SKILL_RECALL_MIN_SIMILARITY = 0.3
# 选中 skill 全文块的 token 预算（进 planner 与合成步骤共用；超预算整条丢弃）。
SKILL_BLOCK_MAX_TOKENS = 6000


def make_tracker(rt: CopilotRuntime) -> BudgetTracker:
    """本 run 四轴账本（编排层创建、下传共用）：召回 embedding / 历史摘要 / 图内 LLM
    全接网关，须先建 tracker 供它们的 run_budget 使用；run 结束导出单位成本落 done 事件。"""
    return BudgetTracker(rt.runtime.budget, sink=rt.runtime.daily_budget)


def make_config(
    rt: CopilotRuntime, run_id: uuid.UUID, conversation_id: uuid.UUID, question: str
) -> dict[str, Any]:
    """组装 graph 运行 config；review 回环在单次 run 内完成，无需分 thread_id。"""
    config: dict[str, Any] = {
        "configurable": {"thread_id": str(run_id)},
        "metadata": {
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "question": question,
        },
    }
    config["callbacks"] = [rt.tracer]
    return config


def finalize_answer(rt: CopilotRuntime, answer_parts: list[str], behavior_score: float) -> str:
    """L5 输出节点：红线阻断 / 综合分分级处置 + 敏感脱敏兜底。

    最终处置只看「最终回答」这一份文本自己的分（内容+来源+行为），不再取 run 级
    最低分兜底——上游低分数据已在 L2 被隔离/脱敏处置，此处不追责。
    """
    final_answer = "".join(answer_parts)
    if is_red_line(final_answer):
        final_answer = "抱歉，我无法提供该回答。"
    else:
        output_score = composite(
            content_trust(final_answer),
            source=source_trust("model"),
            behavior=behavior_score,
        )
        if disposition(output_score) is Disposition.BLOCK:
            final_answer = "抱歉，我无法提供该回答。"
    return redact_sensitive(final_answer)


async def commit_assistant(
    rt: CopilotRuntime,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    answer: str,
    steps: list[dict[str, Any]] | None,
    tracker: BudgetTracker | None,
    intent: str,
    run_state: str = RunState.COMPLETED.value,
) -> CopilotDoneEvent:
    """落库 assistant 消息 + done 事件，返回 done 事件（reactive/plan/resume 共用收尾）。"""
    async with rt.db_lock:
        await rt.chat_repository.add_message(
            ChatMessage(
                id=assistant_message_id,
                conversation_id=conversation_id,
                role=ChatRole.ASSISTANT,
                content=answer,
                steps=steps or None,
            )
        )
    await log(
        rt, run_id, "done", done_payload(rt, assistant_message_id, tracker, intent, run_state)
    )
    return CopilotDoneEvent(assistant_message_id)


def done_payload(
    rt: CopilotRuntime,
    assistant_message_id: uuid.UUID,
    tracker: BudgetTracker | None,
    intent: str,
    run_state: str = RunState.COMPLETED.value,
) -> dict[str, Any]:
    """拼 done 事件 payload：归因维度（intent/model/run_state）+ 账本快照（含缓存命中率）。"""
    payload: dict[str, Any] = {
        "assistant_message_id": str(assistant_message_id),
        "intent": intent,
        "run_state": run_state,
        "model": rt.model_name,
    }
    if tracker is not None:
        accounting = tracker.summary()
        payload["accounting"] = accounting.to_dict()
        maybe_warn_cache(rt, accounting)
    return payload


def maybe_warn_cache(rt: CopilotRuntime, accounting: RunAccounting) -> None:
    """前缀缓存命中率告警：命中率骤降是前缀缓存被破坏的信号（防御层）。"""
    rate = accounting.cache_hit_rate
    if (
        rate is not None
        and accounting.input_tokens >= _CACHE_HIT_MIN_INPUT_TOKENS
        and rate < rt.runtime.cache_hit_rate_warn
    ):
        logger.warning(
            "Copilot 前缀缓存命中率偏低：%.1f%%（input=%d tokens，阈值 %.0f%%），"
            "疑似系统提示或上下文排布被破坏（如往 L0 塞了动态时间戳）",
            rate * 100,
            accounting.input_tokens,
            rt.runtime.cache_hit_rate_warn * 100,
        )


async def log(rt: CopilotRuntime, run_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> None:
    """落一条事件日志；done 后把跨 run 日预算累计外置到 DB（重启续读，防绕过单日上限）。"""
    async with rt.db_lock:
        await rt.event_repository.add_event(
            CopilotEvent(run_id=run_id, type=type_, payload=scrub_payload(payload))
        )
    # 落库失败不阻断响应：预算持久化是 best-effort，失败则本次从内存累计、下次 run 再试。
    if type_ == "done":
        try:
            await rt.runtime.daily_budget.flush()
        except Exception:  # noqa: BLE001 - 持久化失败不阻断已完成响应的 done 事件
            logger.warning("Copilot 日预算落库失败（不影响本轮响应）", exc_info=True)


async def get_or_create_conversation(
    rt: CopilotRuntime, request: CopilotRequest
) -> ChatConversation:
    if request.conversation_id is not None:
        conversation = await rt.chat_repository.get_conversation(request.conversation_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        return conversation
    title = request.question[:TITLE_MAX_LENGTH]
    return await rt.chat_repository.add_conversation(
        ChatConversation(kb_id=None, kind=ChatKind.COPILOT.value, title=title)
    )


async def assemble_context(
    rt: CopilotRuntime, question: str, run_id: uuid.UUID, tracker: BudgetTracker
) -> tuple[str, str, str]:
    """读 soul/user + 召回四型记忆 + 列 skills，组装 L0 系统提示 + L2 记忆块 + L4 重放。

    三执行模式（planner/qa/reactive）共用。召回审计（分路召回日志）在此统一落库——
    任何一条路都能事后查清「某条约束当时为什么没被召回」。约束是确定域（CONSTRAINT
    硬召回全量在场），必须随任务显式携带到 planner/synthesizer/qa，不能只留在
    reactive 的 system prompt 里等着交接时蒸发。
    """
    soul = await rt.memory_store.read("soul")
    user = await rt.memory_store.read("user")
    async with rt.db_lock:
        # 召回 embedding 也经网关（统一门禁/记账/快照），需 run 上下文。
        with run_budget(tracker, run_id=str(run_id)):
            recalled = await rt.memory_service.recall(question)
    await log(
        rt,
        run_id,
        "recall",
        {
            "constraint": len(recalled.constraint),
            "preference": len(recalled.preference),
            "fact": len(recalled.fact),
            "episodic": len(recalled.episodic),
        },
    )
    # 子 Agent 精简约束（父显式下传）：红线 + constraint 型硬约束，spawn_rag 派发时拼入。
    rt.constraints["constraints"] = format_subagent_constraints(recalled)
    # L0 宪法层（system prompt）与 L2 记忆（[MEMORY] 注入块）分离：记忆随本轮 query 变化，
    # 不进永不压缩的 L0，避免挤压系统指令 + 打爆 Prompt Cache 前缀。
    skills, _ = await rt.skill_store.list_skills()
    skill_lines = [f"{s.name}：{s.description or '（无描述）'}" for s in skills]
    system_prompt = assemble_system_prompt(
        soul=soul, user=user, skills=skill_lines, registry=rt.registry
    )
    budget = ContextBudget(rt.runtime.context)
    l0_tokens = estimate_tokens(system_prompt)
    if budget.is_over(Layer.L0, l0_tokens):
        # L0 超 8% 仅告警（文档 §2.1）：system prompt 是永不压缩的固定层，超限不裁剪，
        # 只提示「前缀缓存/系统指令被挤」的风险，处置交给 L5 压缩腾空间。
        logger.warning(
            "L0 system prompt 超预算：%d/%d tokens（仅告警，不裁剪）",
            l0_tokens,
            budget.layer_budget(Layer.L0),
        )
    l2_budget = budget.layer_budget(Layer.L2) or 0  # L2 恒有预算，`or 0` 仅作类型兜底
    memory_block = format_memory_block(recalled, max_tokens=l2_budget)
    reminder = format_reminder()
    return system_prompt, memory_block, reminder


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """两向量余弦相似度（skill 语义召回排序用）；空向量/零向量回退 0（视为不相关）。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def recall_relevant_skills(
    rt: CopilotRuntime, question: str, tracker: BudgetTracker, run_id: str | uuid.UUID
) -> list[CustomSkill]:
    """按 query 语义召回相关 skill（planner 规划期预选，供 generate 与合成步骤注入）。

    skill 存文件系统、无 pgvector 索引，故每次对全量 skill 的 ``name+description`` 做 embedding
    再与 query 余弦排序（skill 数量小，成本可控；后续量大可加 embedding 缓存）。embedding
    经网关统一门禁/记账/快照，须在 ``run_budget`` 内调用。
    """
    skills, _ = await rt.skill_store.list_skills()
    if not skills:
        return []
    with run_budget(tracker, run_id=str(run_id)):
        query_vec = await rt.gateway.embed_query("skill.recall", question)
        docs = [f"{skill.name}：{skill.description or ''}" for skill in skills]
        doc_vecs = await rt.gateway.embed("skill.recall_docs", docs)
    scored = [
        (skill, _cosine_similarity(query_vec, vec))
        for skill, vec in zip(skills, doc_vecs, strict=False)
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [
        skill
        for skill, score in scored[:SKILL_RECALL_TOP_K]
        if score >= SKILL_RECALL_MIN_SIMILARITY
    ]


def wrap_question(rt: CopilotRuntime, question: str) -> str:
    """L1 输入节点：把用户问题包成带可信度的数据块（零信任——用户输入也是不可信方）。

    注入闸恒开，始终打标（``<data trust=... source="user">``）。
    """
    return sanitize_content(question, "user")


async def build_input_messages(
    rt: CopilotRuntime, conversation_id: uuid.UUID, question: str
) -> list[BaseMessage]:
    """装配 L5 history：历史对话（不含本轮问题）+ 本轮问题，只做角色转换。

    六层模型中 L5 是唯一弹性层，历史压缩（_fit_budget / 工具压缩 / 摘要）统一由
    compress 节点的 ``ContextManager`` 按 ratio 驱动（见 agent/runtime/context.py），
    这里不再保留「token 预算 + LLM 摘要」的独立历史逻辑——它已与 L5 五级压缩合并。
    """
    previous = await rt.chat_repository.list_messages(conversation_id)
    # 本轮问题已在 run() 开头落库（作为最后一条），历史只取它之前的部分，
    # 本轮问题在末尾单独 append（经 wrap_question 打标），避免重复注入。
    messages: list[BaseMessage] = [
        HumanMessage(content=message.content)
        if message.role == ChatRole.USER
        else AIMessage(content=message.content)
        for message in previous[:-1]
    ]
    messages.append(HumanMessage(content=wrap_question(rt, question)))
    return messages


async def _stream_message_events(
    rt: CopilotRuntime, run_id: uuid.UUID, session: RunSession, chunk: BaseMessage
) -> AsyncIterator[CopilotStreamEvent]:
    """处理 messages 流消息：AIMessage→tool_call step/记账 + 正文 delta；ToolMessage→结果日志。"""
    if isinstance(chunk, AIMessage):
        for tool_call in chunk.tool_calls or []:
            name = tool_call.get("name")
            args = tool_call.get("args") or {}
            if name:
                session.all_steps.append({"tool_name": name, "args": args})
                call_id = tool_call.get("id")
                if call_id:
                    session.call_name_by_id[call_id] = name
                session.behavior_tracker.record(name, args)
                yield CopilotStepEvent(name, args)
                await log(rt, run_id, "tool_call", {"tool_name": name, "args": args})
        if isinstance(chunk.content, str) and chunk.content:
            session.answer_parts.append(chunk.content)
            yield CopilotDeltaEvent(chunk.content)
    elif isinstance(chunk, ToolMessage):
        name = session.call_name_by_id.get(chunk.tool_call_id, "unknown")
        content = clip(str(chunk.content), TOOL_RESULT_CHARS)
        await log(rt, run_id, "tool_result", {"tool_name": name, "content": content})


async def _stream_approval(
    rt: CopilotRuntime,
    run_id: uuid.UUID,
    session: RunSession,
    interrupt: Any,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
) -> AsyncIterator[CopilotStreamEvent]:
    """命中 interrupt：落审批单 + 挂起 run + 发 approval 事件（消费方据此 return）。"""
    approval = interrupt.value
    tool = approval.get("tool", "")
    args = approval.get("args") or {}
    summary = approval.get("summary", "")
    level = approval.get("level", "high")
    # interrupt.id 是 LangGraph 的 resume map 键；approval_id 是前端逐单回传裁决的主键。
    _interrupt_id = getattr(interrupt, "id", None)
    interrupt_id = str(_interrupt_id) if _interrupt_id is not None else None
    approval_id = (
        uuid.UUID(approval["approval_id"]) if approval.get("approval_id") else uuid.uuid4()
    )
    await record_approval(
        rt,
        run_id,
        approval_id,
        interrupt_id,
        tool,
        args,
        summary,
        level,
        conversation_id,
        assistant_message_id,
    )
    session.run_state = RunState.SUSPENDED.value
    yield CopilotApprovalEvent(approval_id, str(run_id), tool, args, summary, level)


async def _stream_node_updates(
    rt: CopilotRuntime, run_id: uuid.UUID, session: RunSession, updates: dict[str, Any]
) -> AsyncIterator[CopilotStreamEvent]:
    """处理 updates 流的节点更新：压缩日志 + review 节点判定（review/correction delta/终态）。"""
    for node, update in updates.items():
        if "compression_level" in update and update["compression_level"] > 0:
            await log(rt, run_id, "compress", {"level": update["compression_level"]})
        if node != REVIEW_NODE:
            continue
        verdict = update.get("review_verdict", "")
        issues = update.get("review_issues", [])
        yield CopilotReviewEvent(verdict, issues)
        await log(rt, run_id, "review", {"verdict": verdict, "issues": issues})
        if verdict in ("corrected", "unverified"):
            # 仅这两个「结果不可信」的终态带 correction，拼进最终回答并推送
            correction = update.get("correction", "")
            session.answer_parts.append(correction)
            yield CopilotDeltaEvent(correction)
        if verdict != "mismatch":
            session.review_step = {
                "tool_name": REVIEW_TOOL_NAME,
                "args": {"verdict": verdict, "issues": issues},
            }
            session.run_state = RunState.COMPLETED.value


async def stream_graph(
    rt: CopilotRuntime,
    graph: Any,
    config: dict[str, Any],
    run_id: uuid.UUID,
    graph_input: Any,
    session: RunSession,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
) -> AsyncIterator[CopilotStreamEvent]:
    """流式执行图：messages→step/delta、updates→review/approval；命中 interrupt 发 approval。

    ``conversation_id``/``assistant_message_id`` 随审批单一起固化（供刷新后续批回填）。
    """
    try:
        async for mode, payload in graph.astream(
            graph_input, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _metadata = payload
                async for ev in _stream_message_events(rt, run_id, session, chunk):
                    yield ev
            elif mode == "updates":
                if "__interrupt__" in payload:
                    async for ev in _stream_approval(
                        rt,
                        run_id,
                        session,
                        payload["__interrupt__"][0],
                        conversation_id,
                        assistant_message_id,
                    ):
                        yield ev
                    return
                async for ev in _stream_node_updates(rt, run_id, session, payload):
                    yield ev
    except Exception as exc:  # noqa: BLE001 - 兜底：记录错误后上抛，由路由层发 error 事件
        session.run_state = RunState.FAILED.value
        await log(rt, run_id, "error", {"message": str(exc), "run_state": session.run_state})
        raise


async def reject_run(
    rt: CopilotRuntime,
    run_id: uuid.UUID,
    conversation: ChatConversation,
    assistant_message_id: uuid.UUID,
    response: str,
    question: str,
    intent: Any,
) -> AsyncIterator[CopilotStreamEvent]:
    """确定性拒绝：不碰工具/模型，直接回复并落库（注入红线 / 上下文红线共用）。"""
    yield CopilotDeltaEvent(response)
    await log(rt, run_id, "route", {"intent": intent.value, "question": question})
    yield await commit_assistant(
        rt, run_id, conversation.id, assistant_message_id, response, None, None, intent.value
    )


async def record_approval(
    rt: CopilotRuntime,
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    interrupt_id: str | None,
    tool: str,
    args: dict[str, Any],
    summary: str,
    level: str,
    conversation_id: uuid.UUID | None = None,
    assistant_message_id: uuid.UUID | None = None,
) -> None:
    """把高危写工具的 interrupt 落成一张待审审批单（第一类实体，含超时失效）。

    ``approval_id``（前端逐单回传裁决用的主键）与 ``interrupt_id``（LangGraph resume map
    的键）都在挂起那一刻一并固化：前者保证「事件里报的 id」与「持久化主键」一致，后者
    让续批时能把每张单的裁决精确路由到对应的 interrupt。``approval_store`` 恒在场
    （无降级）；落库失败时静默降级——审批仍走 interrupt/resume，只是不持久化审批单、
    无超时，不影响主流程，审批单是可观测性/可恢复性的增强。
    """
    expires_at = datetime.now(UTC) + timedelta(seconds=rt.runtime.approval_timeout_seconds)
    try:
        await rt.approval_store.create(
            CopilotApproval(
                id=approval_id,
                run_id=run_id,
                interrupt_id=interrupt_id,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
                tool=tool,
                args=args,
                summary=summary,
                level=level,
                status=ApprovalStatus.PENDING,
                expires_at=expires_at,
            )
        )
    except Exception:  # noqa: BLE001 - 审批单落库失败不阻断审批主流程
        logger.warning("Copilot 审批单落库失败（降级为纯 interrupt/resume）", exc_info=True)
