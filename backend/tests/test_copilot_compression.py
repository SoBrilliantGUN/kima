"""run 内上下文五级渐进压缩：ratio 计算、级别判定、各级压缩、图内 compress 节点。

对齐「上下文是 RAM 不是硬盘」：L5（history + 工具日志）随轮次累积会稀释注意力，压缩从
最老的历史逐级挤水分，L0-L4（system/state/memory/skills/reminder 固定层）永不压缩。
"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from app.agent.runtime.context import (
    CompressionLevel,
    ContextBudget,
    ContextConfig,
    ContextManager,
    Layer,
    _estimate_ratio,
    format_last_error,
    format_state,
    _pick_level,
)
from tests.fakes import FakeOutputReviewer, ScriptedLLM, gateway_run, make_gateway, make_reactive_graph


def _messages_with_tool_cycles(cycles: int) -> list[BaseMessage]:
    """构造 [q, (AIMessage(tool_call), ToolMessage(result)) * cycles]（纯 L5，无 system）。"""
    messages: list[BaseMessage] = [HumanMessage(content="q")]
    for i in range(cycles):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {"x": str(i)}, "id": f"c{i}"}],
            )
        )
        messages.append(ToolMessage(content=f"result{i}", tool_call_id=f"c{i}"))
    return messages


def test_estimate_ratio() -> None:
    assert _estimate_ratio([], max_tokens=100) == 0.0
    # 全是 CJK 字符："你好" * 100 = 200 字符，ratio 在 (0, 1)
    ratio = _estimate_ratio([HumanMessage(content="你好" * 100)], max_tokens=10_000)
    assert 0.0 < ratio < 1.0
    # fixed_tokens（L0-L4）计入分子
    assert _estimate_ratio([], fixed_tokens=500, max_tokens=1000) == 0.5


def test_pick_level_thresholds() -> None:
    cfg = ContextConfig()
    assert _pick_level(0.1, cfg) == CompressionLevel.NONE
    assert _pick_level(0.6, cfg) == CompressionLevel.TOOL_COMPRESS
    assert _pick_level(0.75, cfg) == CompressionLevel.HISTORY_SUMMARY
    assert _pick_level(0.88, cfg) == CompressionLevel.TOPIC_SUMMARY
    assert _pick_level(0.95, cfg) == CompressionLevel.EMERGENCY


def test_context_budget_layer_limits() -> None:
    """六层预算上限：L0/L1/L2 按 ratio、L3 固定 25k、L4/L5 无预算（文档 §2.2）。"""
    cfg = ContextConfig(max_tokens=1048576)
    b = ContextBudget(cfg)
    assert b.layer_budget(Layer.L0) == int(1048576 * 0.08)
    assert b.layer_budget(Layer.L1) == int(1048576 * 0.15)
    assert b.layer_budget(Layer.L2) == int(1048576 * 0.35)
    assert b.layer_budget(Layer.L3) == cfg.skills_budget
    assert b.layer_budget(Layer.L4) is None
    assert b.layer_budget(Layer.L5) is None


def test_context_budget_is_over() -> None:
    """超限判定：L0 超 8% 判超；L4/L5 无预算恒不超。"""
    b = ContextBudget(ContextConfig(max_tokens=1000))  # L0 预算 = 80
    assert b.is_over(Layer.L0, 81) is True
    assert b.is_over(Layer.L0, 80) is False
    assert b.is_over(Layer.L4, 999_999) is False
    assert b.is_over(Layer.L5, 999_999) is False


def test_format_state_has_state_prefix() -> None:
    block = format_state(turn_count=3, tool_failures=1, last_action="echo", state="running")
    assert block.startswith("[STATE]\n")
    assert "Turn: 3" in block
    assert "State: running" in block
    assert "Failures: 1" in block
    assert "Last action: echo" in block
    assert "Last error:" not in block  # 无失败时不注入 last_error 段


def test_format_last_error() -> None:
    """last_error 折成「工具名 + 类别 + 原因」的一句话信号；无失败返回空串。"""
    assert format_last_error(None) == ""
    assert format_last_error({}) == ""
    assert format_last_error({"kind": "permanent"}) == "permanent（永久失败，勿重试）"
    assert format_last_error({"kind": "transient"}) == "transient（瞬态，可重试）"
    # 带工具名 + 失败原因：三者都在场，且 message 折叠换行
    signal = format_last_error(
        {"tool": "read_note", "kind": "permanent", "message": "该笔记不存在\n请换 id"}
    )
    assert "read_note" in signal
    assert "permanent" in signal
    assert "该笔记不存在" in signal
    assert "\n" not in signal


def test_format_state_with_last_error() -> None:
    """带 last_error 时快照末尾追加 Last error 段（软信号注入）。"""
    block = format_state(
        turn_count=3,
        tool_failures=1,
        last_action="echo",
        state="running",
        last_error=format_last_error(
            {"tool": "read_note", "kind": "permanent", "message": "该笔记不存在"}
        ),
    )
    assert "Last error: read_note" in block
    assert "permanent" in block
    assert "该笔记不存在" in block


async def test_tool_compress_with_summarizer() -> None:
    cfg = ContextConfig(tool_result_min_chars=10)
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["中间摘要"])))
    messages = [
        HumanMessage(content="q"),
        ToolMessage(content="A" * 5000, tool_call_id="c0"),
    ]
    async with gateway_run():
        out = await mgr.compress(messages, CompressionLevel.TOOL_COMPRESS)
    compressed = out[1]
    assert isinstance(compressed, ToolMessage)
    assert compressed.tool_call_id == "c0"
    assert "中间摘要" in str(compressed.content)
    assert len(str(compressed.content)) < 5000


async def test_history_summary_keeps_recent_and_summarizes_older() -> None:
    cfg = ContextConfig(history_recent_msgs=2)
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["历史摘要内容"])))
    messages = _messages_with_tool_cycles(3)  # [q] + 3 轮 = 7 条
    async with gateway_run():
        out = await mgr.compress(messages, CompressionLevel.HISTORY_SUMMARY)
    # summary(1) + 最近 2 条 = 3
    assert len(out) == 3
    assert str(out[0].content).startswith("[HISTORY SUMMARY]")
    assert "历史摘要内容" in str(out[0].content)
    assert out[1].type == "ai"
    assert out[2].type == "tool"


async def test_topic_summary_uses_topic_prefix() -> None:
    cfg = ContextConfig(topic_recent_msgs=2)
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["主题摘要"])))
    messages = _messages_with_tool_cycles(3)
    async with gateway_run():
        out = await mgr.compress(messages, CompressionLevel.TOPIC_SUMMARY)
    assert str(out[0].content).startswith("[TOPIC SUMMARY]")


async def test_emergency_keeps_head_question_and_last_two() -> None:
    cfg = ContextConfig()
    mgr = ContextManager(cfg, summarizer=make_gateway())
    messages = _messages_with_tool_cycles(3)
    out = await mgr.compress(messages, CompressionLevel.EMERGENCY)
    # 无历史摘要时：原始提问（保头）+ 最近 2 条 = 3，首条必须为 user
    assert len(out) == 3
    assert out[0].type == "human"
    assert out[1].type == "ai"
    assert out[2].type == "tool"


class _RecordingModel(FakeMessagesListChatModel):
    """记录每次模型调用收到的消息列表。"""

    received: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    def bind_tools(self, tools: object, **kwargs: object) -> "_RecordingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_graph_compress_node_replaces_messages() -> None:
    """图内 compress 节点：多轮工具后触发压缩，模型收到压缩后的短历史而非全量。"""

    @tool
    async def big_result(x: str) -> str:
        """返回超大结果。"""
        return "X" * 5000

    model = _RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "big_result", "args": {"x": "1"}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "big_result", "args": {"x": "2"}, "id": "c2"}],
            ),
            AIMessage(content="done"),
        ]
    )
    cfg = ContextConfig(max_tokens=50)  # 极小窗口逼出压缩
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["中间摘要"])))
    graph = make_reactive_graph(
        model, [big_result], reviewer=FakeOutputReviewer(), context_manager=mgr
    )
    initial = {
        "messages": [HumanMessage(content="q")],
        "system_prompt": "",
        "memory_block": "",
        "reminder": "",
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
        "compression_level": 0,
    }
    updates = [
        u
        async for u in graph.astream(
            initial, config={"configurable": {"thread_id": "t1"}}, stream_mode="updates"
        )
    ]

    # 三轮 agent 调用都发生
    assert len(model.received) == 3
    # 第三轮前，compress 节点把历史压到「原始提问 + 最近 2 条 + [STATE]」= 4 条
    assert len(model.received[2]) == 4
    assert model.received[2][0].type == "human"  # 原始提问保留（首条为 user）
    assert str(model.received[2][-1].content).startswith("[STATE]")
    # observability：updates 里出现过 compression_level > 0
    compress_levels = [
        u["compress"].get("compression_level", 0)
        for u in updates
        if isinstance(u, dict) and "compress" in u
    ]
    assert any(level > 0 for level in compress_levels)
