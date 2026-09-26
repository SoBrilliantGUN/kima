"""上下文分层（L0/L2）+ 首尾三明治：召回记忆不进 L0、约束提醒钉在上下文末尾。

对齐「上下文是 RAM 不是硬盘」的上下文管理原则：
- L0（系统指令）与 L2（召回记忆）分离——记忆随 query 变化，不该进永不压缩的宪法层。
- 约束提醒（constraint_reminder）每轮钉在模型输入的末尾，历史/工具结果怎么膨胀都压不没。
"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from app.agent.memory import CONSTRAINT_REMINDER, assemble_system_prompt, format_memory_block
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.reactive import build_reactive_graph
from app.models.copilot import CopilotMemory, MemoryKind
from app.services.copilot import RecalledMemories


def _mem(kind: MemoryKind, content: str) -> CopilotMemory:
    return CopilotMemory(kind=kind, content=content)


def _recalled(**kwargs: list[CopilotMemory]) -> RecalledMemories:
    return RecalledMemories(
        constraint=kwargs.get("constraint", []),
        preference=kwargs.get("preference", []),
        fact=kwargs.get("fact", []),
        episodic=kwargs.get("episodic", []),
    )


def test_assemble_system_prompt_excludes_recalled_memories() -> None:
    """召回记忆不再焊进 L0：system prompt 里不能出现记忆原文。"""
    soul = "你说话简洁。"
    user = "用户是后端工程师。"
    prompt = assemble_system_prompt(soul=soul, user=user)
    assert soul in prompt
    assert user in prompt
    assert "一条只该出现在 L2 的记忆内容" not in prompt


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


async def test_constraint_reminder_is_tail_message_every_round() -> None:
    """约束提醒每轮都钉在模型输入末尾，工具结果累积后仍在尾部（不被历史压到中间）。"""

    @tool
    async def echo(x: str) -> str:
        """回显。"""
        return f"echo:{x}"

    reminder = CONSTRAINT_REMINDER
    model = _RecordingModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": "1"}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
    )
    graph = build_reactive_graph(
        model, [echo], runtime=RuntimeConfig(constraint_reminder=reminder)
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    async for _ in graph.astream(initial, stream_mode="updates"):
        pass

    assert len(model.received) == 2
    for messages in model.received:
        assert isinstance(messages[-1], SystemMessage)
        assert messages[-1].content == reminder


async def test_no_reminder_when_disabled() -> None:
    """constraint_reminder 缺省为 None：不回退注入，保持向后兼容。"""
    model = _RecordingModel(responses=[AIMessage(content="ok")])
    graph = build_reactive_graph(model, [])
    human = HumanMessage(content="hi")
    initial = {
        "messages": [human],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    async for _ in graph.astream(initial, stream_mode="updates"):
        pass
    assert len(model.received) == 1
    assert model.received[0][-1] is human  # 末尾仍是用户消息，无提醒
