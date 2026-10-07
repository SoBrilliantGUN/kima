"""run 内上下文压缩 + 六层上下文窗口预算（L0–L5）。

上下文是 RAM 不是硬盘：工具结果 / 历史对话（L5）随轮次累积、把注意力稀释，核心约束（L0）
被推到窗口中间就「在」却不「被看见」。六层窗口（见 ``docs/context-window-layering.md``）中，
L0 system / L1 state / L2 memory / L3 skills / L4 reminder 是**固定成本**（永不压缩、各自独立
预算），L5 history 是唯一弹性层——**压缩只打 L5**。

黄金法则：``ratio = 所有层实际占用之和 / window``，但刀只落在 history——窗口是所有人的池子，
拥挤度要看所有人；能让步的只有 history 一个。``history_budget = window − 其余五层 − margin``。

五级渐进压缩：
- NONE（<0.25）：``_fit_budget`` 丢最老（保持 tool 配对）
- TOOL_COMPRESS（0.25–0.70）：工具结果内联压缩（规则式，无 LLM）
- HISTORY_SUMMARY（0.70–0.85）：保留最近 6 条，更早 LLM 摘要成 ``[HISTORY SUMMARY]``
- TOPIC_SUMMARY（0.85–0.92）：保留最近 4 条，更早 LLM 摘要成 ``[TOPIC SUMMARY]``
- EMERGENCY（≥0.92）：只留最后 2 条 + 最近一条摘要
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.gateway import LLMGateway
from app.chunking.base import estimate_tokens
from app.integrations.llm import ChatMessage as LlmChatMessage

_HISTORY_SUMMARY_PREFIX = "[HISTORY SUMMARY]"
_TOPIC_SUMMARY_PREFIX = "[TOPIC SUMMARY]"
_SUMMARY_SYSTEM = (
    "你是上下文压缩助手。把下面的工具调用记录与对话压缩成一段简短摘要，"
    "保留关键决策、工具调用结果与约束引用。只输出摘要，不要解释。"
)
_TOOL_RESULT_SYSTEM = (
    "你是工具结果压缩助手。把下面的工具返回压缩成一句话摘要，保留关键数据。只输出摘要。"
)


class CompressionLevel(IntEnum):
    """五级渐进压缩：0-4 从保真到保命。"""

    NONE = 0
    TOOL_COMPRESS = 1
    HISTORY_SUMMARY = 2
    TOPIC_SUMMARY = 3
    EMERGENCY = 4


class Layer(StrEnum):
    """六层上下文窗口（对齐 docs/context-window-layering.md）。"""

    L0 = "L0"  # system prompt（宪法 + soul/user + 操作引导 + skills 列表）
    L1 = "L1"  # [STATE] 状态快照
    L2 = "L2"  # [MEMORY] 记忆 + RAG 结论
    L3 = "L3"  # [INVOKED SKILLS] 已加载 skills 全文
    L4 = "L4"  # [REMINDER] 宪法尾部重放
    L5 = "L5"  # history + 工具日志（唯一压缩对象）


class RunState(StrEnum):
    """run 生命周期（L1 快照 ``State`` 字段的真实值）。"""

    RUNNING = "running"  # agent ⇄ tools ⇄ review 循环进行中
    SUSPENDED = "suspended"  # HITL interrupt 挂起，等写工具审批
    COMPLETED = "completed"  # review 判终 / plan 完成 / 拒绝分支
    FAILED = "failed"  # 异常逃出图（熔断/预算/未知错误）
    INTERRUPTED = "interrupted"  # 前端中断（abort），已落库部分回答


@dataclass(frozen=True)
class ContextConfig:
    """上下文窗口预算 + 各压缩级别触发阈值（ratio = 所有层之和 / max_tokens）。"""

    max_tokens: int = 1048576  # window：DeepSeek V4 官方 1M
    l0_ratio: float = 0.08  # L0 system（超仅告警）
    l1_ratio: float = 0.15  # L1 state（超仅告警）
    l2_ratio: float = 0.35  # L2 memory + RAG（超裁剪）
    skills_budget: int = 25000  # L3 skills 全文（独立固定，超截断）
    safety_margin: int = 500  # L5 计算时预留的安全余量
    tool_compress_at: float = 0.25
    history_summary_at: float = 0.70
    topic_summary_at: float = 0.85
    emergency_at: float = 0.92
    history_recent_msgs: int = 6  # HISTORY_SUMMARY 保留的最近条数（verbatim）
    topic_recent_msgs: int = 4  # TOPIC_SUMMARY 保留的最近条数（verbatim）
    tool_result_min_chars: int = 2000  # 单个 tool result 触发内联压缩的长度阈值
    tool_result_head_chars: int = 500
    tool_result_tail_chars: int = 200


def format_last_error(last_error: dict[str, Any] | None, max_chars: int = 200) -> str:
    """把 ``last_error`` 折成给模型的一句话信号：工具名 + 类别 + 失败原因（空 = 无失败）。

    ``last_error`` 是工具层 ``with_retry`` 重试耗尽后仍失败的最终现场
    （``{"tool","message","kind"}``），``kind`` 由 :func:`classify_error` 产出
    （transient/permanent）。``message`` 是 ``str(exc)``——对 ToolFailure 即红绿灯文本
    （含原因/建议），折叠换行并截断后注入 L1 快照：快照永不压缩，ToolMessage 里的红绿灯
    文本被压缩后信号仍在场。
    """
    if not last_error:
        return ""
    kind = last_error.get("kind", "")
    tool = str(last_error.get("tool", "")).strip()
    message = " ".join(str(last_error.get("message", "")).split())
    if len(message) > max_chars:
        message = message[:max_chars] + "…"
    if kind == "permanent":
        label = "permanent（永久失败，勿重试）"
    elif kind == "transient":
        label = "transient（瞬态，可重试）"
    else:
        label = str(kind)
    parts = [p for p in (tool, label, message) if p]
    return " · ".join(parts)


def format_state(
    *,
    turn_count: int,
    tool_failures: int,
    last_action: str,
    state: str,
    last_error: str = "",
) -> str:
    """L1 状态快照（``[STATE]`` 带内标记）：轻量运行时状态，不是完整 State JSON（噪音太大）。

    ``state`` 是 run 的真实生命周期状态（``RunState`` 值），由调用方从图状态读出，
    而不是写死 ``"running"``。
    ``last_error`` 是经 :func:`format_last_error` 映射后的失败信号（空则省略该段）。
    """
    line = (
        f"[STATE]\nTurn: {turn_count} | State: {state} | "
        f"Failures: {tool_failures} | Last action: {last_action or 'none'}"
    )
    if last_error:
        line += f" | Last error: {last_error}"
    return line


def _count_message(message: BaseMessage) -> int:
    """单条消息的 token 估算（含约 4 token 协议开销）。"""
    return estimate_tokens(str(message.content)) + 4


def _estimate_ratio(
    messages: Sequence[BaseMessage], fixed_tokens: int = 0, max_tokens: int = 0
) -> float:
    """当前上下文 token 占窗口预算的比例（fixed_tokens = L0+L1+L2+L3+L4 之和）。"""
    if max_tokens <= 0:
        return 0.0
    total = fixed_tokens + sum(_count_message(m) for m in messages)
    return total / max_tokens


def _pick_level(ratio: float, cfg: ContextConfig) -> CompressionLevel:
    """按 ratio 命中第一个压缩级别（阈值升序，只升不降）。"""
    if ratio < cfg.tool_compress_at:
        return CompressionLevel.NONE
    if ratio < cfg.history_summary_at:
        return CompressionLevel.TOOL_COMPRESS
    if ratio < cfg.topic_summary_at:
        return CompressionLevel.HISTORY_SUMMARY
    if ratio < cfg.emergency_at:
        return CompressionLevel.TOPIC_SUMMARY
    return CompressionLevel.EMERGENCY


class ContextBudget:
    """六层预算：各层独立上限 + 超限判定（L4 无预算、L5 走余量不进本表）。

    是 L0/L1/L2/L3 预算上限的**单一权威来源**：装配点问 ``layer_budget`` 拿上限、
    ``is_over`` 判超限，再各自处置——L0/L1 超限仅告警、L2 裁剪（``format_memory_block``
    按预算贪心填充）、L3 截断（``_build_invoked_skills_block`` 按预算丢整条）。
    L4 无预算（宪法尾部重放不设上限），L5 走余量（``ContextManager.history_budget``）
    不进本表。
    """

    def __init__(self, cfg: ContextConfig, max_tokens: int | None = None) -> None:
        self._cfg = cfg
        self._max = max_tokens if max_tokens is not None else cfg.max_tokens

    def layer_budget(self, layer: Layer) -> int | None:
        """各层预算上限；L4 返回 None（无预算）、L5 返回 None（余量，压缩管理）。"""
        if layer == Layer.L0:
            return int(self._max * self._cfg.l0_ratio)
        if layer == Layer.L1:
            return int(self._max * self._cfg.l1_ratio)
        if layer == Layer.L2:
            return int(self._max * self._cfg.l2_ratio)
        if layer == Layer.L3:
            return self._cfg.skills_budget
        return None  # L4 / L5

    def is_over(self, layer: Layer, tokens: int) -> bool:
        """某层已花 ``tokens`` 是否超其预算上限；L4/L5 无预算恒返回 False。"""
        budget = self.layer_budget(layer)
        return budget is not None and tokens > budget


def _group_tool_pairs(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    """把 tool_use / tool_result 配成原子组，避免截断时拆散配对。"""
    groups: list[list[BaseMessage]] = []
    open_tool_ids: set[str] = set()
    current_group: list[BaseMessage] | None = None

    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            if current_group is not None:
                groups.append(current_group)
            current_group = [msg]
            open_tool_ids = {str(tc.get("id", "")) for tc in msg.tool_calls}
        elif isinstance(msg, ToolMessage):
            call_id = str(msg.tool_call_id)
            if current_group is not None and call_id in open_tool_ids:
                current_group.append(msg)
            else:
                if current_group is not None:
                    groups.append(current_group)
                    current_group = None
                    open_tool_ids = set()
                groups.append([msg])
        else:
            if current_group is not None:
                groups.append(current_group)
                current_group = None
                open_tool_ids = set()
            groups.append([msg])

    if current_group is not None:
        groups.append(current_group)
    return groups


def _fit_within_budget(
    items: Sequence[list[BaseMessage]], budget: int, token_of: object
) -> list[list[BaseMessage]]:
    """从尾部保留最近的消息组，直到塞满 ``budget``（最老的最先丢）。"""
    remaining = budget
    kept: list[list[BaseMessage]] = []
    for group in reversed(items):
        cost = token_of(group)  # type: ignore[operator]
        if cost > remaining and kept:
            break
        kept.append(group)
        remaining -= cost
    kept.reverse()
    return kept


def _fit_budget(messages: Sequence[BaseMessage], budget: int) -> list[BaseMessage]:
    """按 token 预算丢最老（保持 tool 配对），返回新列表。

    保头：首组若是纯 user（原始提问），即便超出预算也保留——保证压缩后首条仍为 user
    （Anthropic/DeepSeek 要求首条非 system 必须是 user），且不丢用户原始问题。
    """
    if budget < 0:
        budget = 0
    groups = _group_tool_pairs(messages)
    kept = _fit_within_budget(groups, budget, lambda g: sum(_count_message(m) for m in g))
    head = groups[0] if groups else None
    if head is not None and len(head) == 1 and isinstance(head[0], HumanMessage):
        if not kept or kept[0] is not head:
            kept = [head, *kept]
    return [msg for group in kept for msg in group]


def _safe_tail_start(messages: Sequence[BaseMessage], recent_msgs: int) -> int:
    """返回最近 ``recent_msgs`` 条 message 尾部的起点下标，排除孤儿ToolMessage"""
    idx = max(0, len(messages) - recent_msgs)
    while 0 < idx < len(messages) and isinstance(messages[idx], ToolMessage):
        idx -= 1
    return idx


class ContextManager:
    """六层上下文装配的「压缩器」：只压缩 L5 history，ratio 用所有层之和。

    构造时只需窗口配置与摘要器；每次调用 ``current_level`` / ``compress`` 时，由调用方
    （reactive 的 compress 节点）传入 ``fixed_tokens``（L0+L1+L2+L3+L4 之和）与
    ``history_budget``（L5 可用空间）。L0/L1/L2/L4 的文本内容由 service / agent 节点持有，
    不在本类里装配。
    """

    def __init__(self, cfg: ContextConfig, summarizer: LLMGateway) -> None:
        self._cfg = cfg
        self._summarizer = summarizer

    def history_budget(self, fixed_tokens: int) -> int:
        """L5 可用空间 = window − 其余五层之和 − safety_margin。"""
        return max(0, self._cfg.max_tokens - fixed_tokens - self._cfg.safety_margin)

    def current_level(
        self, messages: Sequence[BaseMessage], fixed_tokens: int = 0
    ) -> CompressionLevel:
        """按当前消息列表 + 固定层占用算出该触发的压缩级别。"""
        return _pick_level(_estimate_ratio(messages, fixed_tokens, self._cfg.max_tokens), self._cfg)

    async def compress(
        self,
        messages: Sequence[BaseMessage],
        level: CompressionLevel,
        history_budget: int | None = None,
    ) -> list[BaseMessage]:
        """按级别压缩 L5 history。``NONE`` 只 _fit_budget，其余返回新列表。"""
        budget = history_budget if history_budget is not None else self.history_budget(0)
        if level == CompressionLevel.NONE:
            return _fit_budget(messages, budget)
        if level == CompressionLevel.TOOL_COMPRESS:
            return _fit_budget(await self._compress_tool_results(messages), budget)
        if level == CompressionLevel.HISTORY_SUMMARY:
            return await self._summarize_older(
                messages, self._cfg.history_recent_msgs, _HISTORY_SUMMARY_PREFIX
            )
        if level == CompressionLevel.TOPIC_SUMMARY:
            return await self._summarize_older(
                messages, self._cfg.topic_recent_msgs, _TOPIC_SUMMARY_PREFIX
            )
        return self._emergency(messages)

    # --- Level 1：工具结果内联压缩（首 + 尾 + 中间一句话摘要） ---

    async def _compress_tool_results(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and len(str(message.content)) > self._cfg.tool_result_min_chars
            ):
                content = await self._compress_tool_result(str(message.content))
                result.append(ToolMessage(content=content, tool_call_id=message.tool_call_id))
            else:
                result.append(message)
        return result

    async def _compress_tool_result(self, content: str) -> str:
        head = content[: self._cfg.tool_result_head_chars]
        tail = content[-self._cfg.tool_result_tail_chars :]
        middle = content[
            self._cfg.tool_result_head_chars : len(content) - self._cfg.tool_result_tail_chars
        ]
        response = await self._summarizer.complete(
            "compress",
            [LlmChatMessage("system", _TOOL_RESULT_SYSTEM), LlmChatMessage("user", middle)],
            temperature=0,
        )
        summary = response.content.strip()
        return f"{head}\n…[中间摘要] {summary}\n…{tail}"

    # --- Level 2/3：更早轮次摘要（保留最近 N 条消息） ---

    async def _summarize_older(
        self, messages: Sequence[BaseMessage], keep_msgs: int, prefix: str
    ) -> list[BaseMessage]:
        idx = _safe_tail_start(messages, keep_msgs)
        older = messages[:idx]
        recent = list(messages[idx:])
        if not older:
            return recent
        summary = await self._summarize_middle(older)
        if summary is None:
            return recent  # 无摘要器兜底：丢弃中间段
        return [HumanMessage(content=f"{prefix}\n{summary}"), *recent]

    async def _summarize_middle(self, middle: Sequence[BaseMessage]) -> str | None:
        if not middle:
            return None
        text = "\n".join(f"{_role(m)}: {str(m.content)[:600]}" for m in middle)
        response = await self._summarizer.complete(
            "compress",
            [LlmChatMessage("system", _SUMMARY_SYSTEM), LlmChatMessage("user", text)],
            temperature=0,
        )
        summary = response.content.strip()
        return summary or None

    # --- Level 4：紧急模式（最后 2 条 + 最近一条摘要） ---

    def _emergency(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        emergency_msgs = list(messages[_safe_tail_start(messages, 2) :])

        summary_msg: BaseMessage | None = None
        for msg in reversed(messages):
            content = str(msg.content)
            if content.startswith(_HISTORY_SUMMARY_PREFIX) or content.startswith(
                _TOPIC_SUMMARY_PREFIX
            ):
                summary_msg = msg
                break

        if summary_msg is not None:
            return [summary_msg, *emergency_msgs]
        # 无既有摘要：保头原始提问，保证首条非 system 是 user（丢头会以 assistant/tool 开头）。
        head = messages[0] if messages else None
        if (
            head is not None
            and isinstance(head, HumanMessage)
            and (not emergency_msgs or emergency_msgs[0] is not head)
        ):
            emergency_msgs = [head, *emergency_msgs]
        return emergency_msgs


def _role(message: BaseMessage) -> str:
    if isinstance(message, ToolMessage):
        return "tool"
    return message.type
