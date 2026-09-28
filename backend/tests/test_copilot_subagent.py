"""RAG 子 Agent（SubAgent）：独立窗口检索、只回结论、契约截断。"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from app.agent.runtime.rag_subagent import RagSubagent


class _RecordingModel(FakeMessagesListChatModel):
    """脚本化模型：按 responses 顺序回放，记录收到的消息。"""

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


async def test_rag_subagent_multi_turn_returns_conclusion() -> None:
    """子图多轮：第一轮调工具，第二轮回结论；只回结论文本。"""

    @tool
    async def echo(x: str) -> str:
        """回显。"""
        return f"echo:{x}"

    model = _RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"x": "1"}, "id": "c1"}],
            ),
            AIMessage(content="结论：检索到 1 条。"),
        ]
    )
    sub = RagSubagent(model, [echo], max_turns=3)
    result = await sub.run("检索并总结")
    assert result == "结论：检索到 1 条。"
    assert len(model.received) == 2


async def test_rag_subagent_truncates_long_result() -> None:
    """契约截断：超长结论按 max_result_chars 截断。"""
    model = _RecordingModel(responses=[AIMessage(content="X" * 5000)])
    sub = RagSubagent(model, [], max_turns=3, max_result_chars=100)
    result = await sub.run("任务")
    assert len(result) <= 101  # 100 字符 + 省略号
    assert result.endswith("…")


async def test_rag_subagent_injects_system_prompt() -> None:
    """子图每轮注入 RAG system 提示（独立窗口）。"""
    model = _RecordingModel(responses=[AIMessage(content="done")])
    sub = RagSubagent(model, [], max_turns=3)
    await sub.run("任务")
    assert len(model.received) == 1
    assert model.received[0][0].type == "system"
