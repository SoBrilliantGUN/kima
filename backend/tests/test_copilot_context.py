"""上下文六层分层（L0/L2/L4）+ 首尾三明治：召回记忆不进 L0、宪法尾部重放钉在末尾。

对齐「上下文是 RAM 不是硬盘」的上下文管理原则：
- L0（宪法 + soul/user + 操作引导）与 L2（召回记忆）分离——记忆随 query 变化，不进永不压缩的 L0。
- L4（``[REMINDER]`` 宪法尾部重放）每轮钉在模型输入末尾，历史/工具结果怎么膨胀都压不没。
"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from app.agent.memory import (
    assemble_system_prompt,
    format_memory_block,
    format_reminder,
    load_constitution,
)
from app.agent.runtime.reactive import build_reactive_graph
from app.models.copilot import CopilotMemory, MemoryKind
from app.services.copilot import RecalledMemories
from tests.fakes import FakeOutputReviewer


def _mem(kind: MemoryKind, content: str) -> CopilotMemory:
    return CopilotMemory(kind=kind, content=content)


def _recalled(**kwargs: list[CopilotMemory]) -> RecalledMemories:
    return RecalledMemories(
        constraint=kwargs.get("constraint", []),
        preference=kwargs.get("preference", []),
        fact=kwargs.get("fact", []),
        episodic=kwargs.get("episodic", []),
    )


def test_load_constitution_non_empty() -> None:
    assert load_constitution().strip()


def test_assemble_system_prompt_includes_constitution_and_excludes_memories() -> None:
    """宪法拼进 L0；召回记忆不进 L0。"""
    soul = "你说话简洁。"
    user = "用户是后端工程师。"
    prompt = assemble_system_prompt(soul=soul, user=user)
    assert soul in prompt
    assert user in prompt
    assert load_constitution() in prompt
    assert "一条只该出现在 L2 的记忆内容" not in prompt


def test_format_reminder_wraps_with_reminder_prefix() -> None:
    reminder = format_reminder()
    assert reminder.startswith("[REMINDER]\n")
    assert load_constitution() in reminder


def test_format_memory_block_empty() -> None:
    assert format_memory_block(_recalled()) == ""


def test_format_memory_block_wraps_with_memory_prefix() -> None:
    block = format_memory_block(
        _recalled(
            preference=[_mem(MemoryKind.PREFERENCE, "回答要简洁")],
            fact=[_mem(MemoryKind.FACT, "用户是产品经理")],
            episodic=[_mem(MemoryKind.EPISODIC, "昨天讨论了架构")],
        )
    )
    assert block.startswith("[MEMORY]\n")
    assert "回答要简洁" in block
    assert "用户是产品经理" in block
    assert "昨天讨论了架构" in block


class _RecordingModel(FakeMessagesListChatModel):
    """记录每次模型调用收到的消息列表（首尾三明治断言用）。"""

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


def _initial(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "messages": [HumanMessage(content="hi")],
        "system_prompt": "你是助手。",
        "memory_block": "",
        "reminder": "",
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    base.update(overrides)
    return base


async def test_reminder_is_tail_message_every_round() -> None:
    """L4 宪法重放每轮钉在模型输入末尾，工具结果累积后仍在尾部（不被历史压到中间）。"""

    @tool
    async def echo(x: str) -> str:
        """回显。"""
        return f"echo:{x}"

    reminder = format_reminder()
    model = _RecordingModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": "1"}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
    )
    graph = build_reactive_graph(model, [echo], reviewer=FakeOutputReviewer())
    async for _ in graph.astream(_initial(reminder=reminder), stream_mode="updates"):
        pass

    assert len(model.received) == 2
    for messages in model.received:
        assert isinstance(messages[-1], HumanMessage)
        assert messages[-1].content == reminder  # 尾部是 [REMINDER]，user 角色


async def test_state_block_precedes_reminder() -> None:
    """装配顺序：history → (memory) → state → reminder，state 在 reminder 之前。"""
    model = _RecordingModel(responses=[AIMessage(content="ok")])
    graph = build_reactive_graph(model, [], reviewer=FakeOutputReviewer())
    async for _ in graph.astream(
        _initial(reminder="[REMINDER]\n宪法正文"), stream_mode="updates"
    ):
        pass
    assert len(model.received) == 1
    received = model.received[0]
    assert str(received[-1].content).startswith("[REMINDER]")
    assert str(received[-2].content).startswith("[STATE]")
