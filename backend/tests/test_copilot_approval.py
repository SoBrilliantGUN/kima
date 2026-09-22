"""HITL：写工具 interrupt 挂起 → Command(resume=...) 重放同一调用。"""

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.reactive import build_reactive_graph
from app.agent.toolmeta import SideEffectLevel, ToolMeta

# 把 create_note 标为写工具（MEDIUM），使 HITL 门禁生效（不强制幂等，测试工具无该参数）
_WRITE_REGISTRY = {
    "create_note": ToolMeta("create_note", SideEffectLevel.MEDIUM, "tool_result", 500)
}


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> "Model":
        return self


def _initial() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="写个笔记")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }


async def test_write_tool_interrupt_and_approve() -> None:
    calls: list[str] = []

    @tool
    async def create_note(title: str, content: str) -> str:
        """写笔记。"""
        calls.append(title)
        return f"created {title}"

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "create_note", "args": {"title": "t", "content": "c"}, "id": "c1"}
                ],
            ),
            AIMessage(content="写好了。"),
        ]
    )
    graph = build_reactive_graph(
        model,
        [create_note],
        checkpointer=InMemorySaver(),
        runtime=RuntimeConfig(require_write_approval=True),
        registry=_WRITE_REGISTRY,
    )
    config = {"configurable": {"thread_id": "t1"}}

    # 第一次 run：命中 interrupt（写工具需确认）
    chunks = [c async for c in graph.astream(_initial(), config=config, stream_mode="updates")]
    assert any("__interrupt__" in c for c in chunks)
    assert calls == []  # 尚未执行

    # resume(approve)：重放同一调用，工具执行 + 最终回答
    resumed = [
        c
        async for c in graph.astream(
            Command(resume="approve"), config=config, stream_mode="updates"
        )
    ]
    assert calls == ["t"]
    assert any("agent" in c for c in resumed)


async def test_write_tool_interrupt_and_reject() -> None:
    calls: list[str] = []

    @tool
    async def create_note(title: str, content: str) -> str:
        """写笔记。"""
        calls.append(title)
        return f"created {title}"

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "create_note", "args": {"title": "t", "content": "c"}, "id": "c1"}
                ],
            ),
            AIMessage(content="好的。"),
        ]
    )
    graph = build_reactive_graph(
        model,
        [create_note],
        checkpointer=InMemorySaver(),
        runtime=RuntimeConfig(require_write_approval=True),
        registry=_WRITE_REGISTRY,
    )
    config = {"configurable": {"thread_id": "t2"}}

    chunks = [c async for c in graph.astream(_initial(), config=config, stream_mode="updates")]
    assert any("__interrupt__" in c for c in chunks)

    # resume(reject)：工具不执行，返回拒绝 ToolMessage
    resumed = [
        c
        async for c in graph.astream(
            Command(resume="reject"), config=config, stream_mode="updates"
        )
    ]
    assert calls == []  # 工具未被调用
    assert any("tools" in c for c in resumed)
