"""run 内上下文五级渐进压缩：ratio 计算、级别判定、各级压缩、图内 compress 节点。

对齐「上下文是 RAM 不是硬盘」：工具结果/历史（L3）随轮次累积会稀释注意力，压缩从
最老的工具日志逐级挤水分，L0（system）/ 最近几轮决策永远不动。
"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from app.agent.runtime.context import (
    CompressionLevel,
    ContextConfig,
    ContextManager,
    estimate_ratio,
    pick_level,
)
from app.agent.runtime.reactive import build_reactive_graph
from tests.fakes import ScriptedLLM, gateway_run, make_gateway


def _messages_with_tool_cycles(cycles: int) -> list[BaseMessage]:
    """构造 [system, q, (AIMessage(tool_call), ToolMessage(result)) * cycles]。"""
    messages: list[BaseMessage] = [SystemMessage(content="system"), HumanMessage(content="q")]
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
    assert estimate_ratio([], max_tokens=100) == 0.0
    # 全是 CJK 字符："你好" * 100 = 200 token + 每条消息 4 token 开销
    ratio = estimate_ratio([HumanMessage(content="你好" * 100)], max_tokens=10_000)
    assert 0.0 < ratio < 1.0


def test_pick_level_thresholds() -> None:
    cfg = ContextConfig()
    assert pick_level(0.1, cfg) == CompressionLevel.NONE
    assert pick_level(0.6, cfg) == CompressionLevel.TOOL_COMPRESS
    assert pick_level(0.75, cfg) == CompressionLevel.HISTORY_SUMMARY
    assert pick_level(0.88, cfg) == CompressionLevel.TOPIC_SUMMARY
    assert pick_level(0.95, cfg) == CompressionLevel.EMERGENCY


async def test_tool_compress_with_summarizer() -> None:
    cfg = ContextConfig(tool_result_min_chars=10)
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["中间摘要"])))
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="q"),
        ToolMessage(content="A" * 5000, tool_call_id="c0"),
    ]
    async with gateway_run():
        out = await mgr.compress(messages, CompressionLevel.TOOL_COMPRESS)
    compressed = out[2]
    assert isinstance(compressed, ToolMessage)
    assert compressed.tool_call_id == "c0"
    assert "中间摘要" in str(compressed.content)
    assert len(str(compressed.content)) < 5000


async def test_tool_compress_without_summarizer_truncates() -> None:
    cfg = ContextConfig(tool_result_min_chars=10)
    mgr = ContextManager(cfg)  # 无摘要器 → 只保留首尾
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="q"),
        ToolMessage(content="A" * 5000, tool_call_id="c0"),
    ]
    out = await mgr.compress(messages, CompressionLevel.TOOL_COMPRESS)
    assert "中间已截断" in str(out[2].content)
    assert len(str(out[2].content)) < 5000


async def test_history_summary_keeps_head_and_recent_turn() -> None:
    cfg = ContextConfig(recent_turns=1)
    mgr = ContextManager(cfg, summarizer=make_gateway(llm=ScriptedLLM(contents=["历史摘要内容"])))
    messages = _messages_with_tool_cycles(3)  # [system, q] + 3 轮工具 = 8 条
    async with gateway_run():
        out = await mgr.compress(messages, CompressionLevel.HISTORY_SUMMARY)
    # head(2) + summary(1) + tail(最近 1 轮 = 2) = 5
    assert len(out) == 5
    assert out[0].type == "system"
    assert out[1].type == "human"  # 本轮问题保留
    assert "历史摘要内容" in str(out[2].content)  # 中间被摘要
    assert out[3].type == "ai"
    assert out[4].type == "tool"


async def test_emergency_drops_middle() -> None:
    cfg = ContextConfig()
    mgr = ContextManager(cfg)
    messages = _messages_with_tool_cycles(3)
    out = await mgr.compress(messages, CompressionLevel.EMERGENCY)
    assert len(out) == 4  # head(2) + tail(最近 1 轮 = 2)
    assert out[0].type == "system"
    assert out[1].type == "human"
    assert out[2].type == "ai"
    assert out[3].type == "tool"


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
    mgr = ContextManager(cfg)  # 无摘要器 → 丢弃中间
    graph = build_reactive_graph(model, [big_result], context_manager=mgr)
    initial = {
        "messages": [HumanMessage(content="q")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
        "trust": 100.0,
        "compression_level": 0,
    }
    updates = [u async for u in graph.astream(initial, stream_mode="updates")]

    # 三轮 agent 调用都发生
    assert len(model.received) == 3
    # 第三轮前，compress 节点把 5 条历史压到 3 条（头 1 + 最近 1 轮 2）
    assert len(model.received[2]) == 3
    assert model.received[2][0].type == "human"  # 问题保留
    # observability：updates 里出现过 compression_level > 0
    compress_levels = [
        u["compress"].get("compression_level", 0)
        for u in updates
        if isinstance(u, dict) and "compress" in u
    ]
    assert any(level > 0 for level in compress_levels)
