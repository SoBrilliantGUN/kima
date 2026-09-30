"""RAG 子 Agent：复用主循环图（``build_reactive_graph``）的独立窗口检索子 Agent。

子 Agent 与主 Agent 是「对等完整版」——**同一套 agent ⇄ tools ⇄ review 图**，仅靠调用参数
区分：工具白名单（只读检索五件套、不含 ``spawn_rag`` 防递归）、独立初始 state、共享成本 +
独立 turn（``count_turn=False``，不挤占主循环 ``max_turns``）。

图在 ``run`` 时动态构建：需从 ContextVar 取主循环的 tracker/run_id（共享成本 + 同一 run
归因），故不能跨 run 缓存。``spawn_rag`` 工具在 tool 节点被 ``run_budget`` 包裹，``run``
里 ``current()`` 拿到的就是主循环的 run context；无网关的测试路径则自建独立账本。

约束由父 Agent **显式下传**（Claude 强隔离，唯一通道是派发参数）：``run(task, constraints)``
的 ``constraints`` 是调用方（``spawn_rag``）拼好的「红线 + constraint 型硬约束」，子 Agent
不自动继承父上下文的 soul/user 人设或完整记忆块。
"""

import uuid

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.gateway import LLMGateway
from app.agent.gateway_context import current
from app.agent.guardrail.review import OutputReviewer, SideEffectVerifier
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.budget import BudgetTracker
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.context import RunState
from app.agent.runtime.reactive import build_reactive_graph
from app.agent.toolmeta import ToolRegistry

_RAG_SYSTEM = (
    "你是知识库检索子 Agent。根据任务，检索知识库、阅读文档/笔记，必要时多轮检索补充，"
    "最后给出简明结论。只输出结论，不要解释检索过程。"
)


class RagSubagent:
    """RAG 子 Agent 门面：``spawn_rag`` 工具调它，复用主循环图跑完只回结论（契约截断）。

    与主 Agent 对等（含 review 自检 / 约束 / 安全闸），仅工具白名单 + 独立 turn 区分。
    图在 ``run`` 时动态构建（复用 ``build_reactive_graph`` + 主循环 tracker/run_id）。
    """

    def __init__(
        self,
        model: BaseChatModel,
        tools: list[BaseTool],
        registry: ToolRegistry,
        *,
        reviewer: OutputReviewer,
        runtime: RuntimeConfig,
        verifier: SideEffectVerifier,
        breaker: CircuitBreaker,
        security_breaker: SecurityBreaker,
        review_max_attempts: int = 2,
        gateway: LLMGateway,
        max_turns: int = 4,
        max_result_chars: int = 2000,
    ) -> None:
        self._model = model
        self._tools = tools
        self._registry = registry
        self._reviewer = reviewer
        self._runtime = runtime
        self._verifier = verifier
        self._breaker = breaker
        self._security_breaker = security_breaker
        self._review_max_attempts = review_max_attempts
        self._gateway = gateway
        self._max_turns = max_turns
        self._max_result_chars = max_result_chars

    async def run(self, task: str, constraints: str = "") -> str:
        """复用主循环图跑子任务（共享主循环 tracker/run_id、独立 turn），截断后回结论。"""
        try:
            ctx = current()  # spawn_rag 工具在 run_budget 里，此处即主循环 run context
            tracker = ctx.tracker
            run_id = ctx.run_id
        except RuntimeError:
            # 测试/无网关路径：无 run_budget 上下文，自建独立账本
            tracker = BudgetTracker(self._runtime.budget, sink=self._runtime.daily_budget)
            run_id = str(uuid.uuid4())

        graph = build_reactive_graph(
            self._model,
            self._tools,
            reviewer=self._reviewer,
            checkpointer=InMemorySaver(),  # 子 Agent 只读无 HITL，内存版足够（不持久化）
            review_max_attempts=self._review_max_attempts,
            runtime=self._runtime,
            verifier=self._verifier,
            registry=self._registry,
            breaker=self._breaker,
            security_breaker=self._security_breaker,
            context_manager=None,  # 子 Agent 独立短窗口，不压缩
            gateway=self._gateway,
            tracker=tracker,  # 共享成本：记入主循环 tracker
            count_turn=False,  # 独立 turn：不挤占主循环 max_turns
        )
        system_prompt = _RAG_SYSTEM + (f"\n\n{constraints}" if constraints else "")
        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content=task)],
                "system_prompt": system_prompt,
                "memory_block": "",
                "reminder": "",
                "run_state": RunState.RUNNING.value,
                "turn_count": 0,
                "tool_failures": 0,
                "last_action": "",
                "attempts": 0,
                "review_verdict": "",
                "review_issues": [],
                "correction": "",
                "compression_level": 0,
                "last_error": None,
            },
            config={
                "configurable": {"thread_id": run_id},
                "recursion_limit": self._max_turns * 2 + 2,
            },
        )
        final = result["messages"][-1]
        content = str(final.content)
        if len(content) > self._max_result_chars:
            content = content[: self._max_result_chars] + "…"
        return content
