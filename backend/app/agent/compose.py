"""Copilot 运行时装配：把散落的 24 个依赖收敛成一个 `CopilotRuntime` 组合对象。

`CopilotService` 是「上帝构造函数」的根源：它把 `build_tools` / `build_reactive_graph`
两套装配的原料清单全摊在自己的 `__init__` 签名里，再转手喂给装配函数。本模块把装配
收敛到一处：`build_runtime` 一次性完成「工具装配 + 图装配绑定 + 派生缓存 + 共享可变
状态」，入口函数（`run` / `resume`）只收成品 `CopilotRuntime` + 请求参数，不再裸收一长串。

`CopilotRuntime` 是纯数据容器（无方法、无继承），字段分三类：
- 编排消费：入口函数 / 共用编排函数真正读的依赖（原第③组 + 策略）；
- 装配成品：`build_tools` / `build_reactive_graph` 的产物，入口不再关心原料；
- 共享可变状态：原来靠「同一实例属性」跨节点共享的 dict / lock，函数化后显式收拢。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.agent.gateway import LLMGateway
from app.agent.guardrail.review import OutputReviewer, SideEffectVerifier
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.context import ContextManager
from app.agent.runtime.planner import Planner
from app.agent.runtime.rag_subagent import RagSubagent
from app.agent.runtime.reactive import build_reactive_graph
from app.agent.toolmeta import ToolRegistry
from app.agent.tools import build_tools
from app.core.memory_store import MemoryFileStore
from app.core.skill_store import SkillFileStore
from app.integrations.search import WebSearchClient
from app.rag.retriever import RagRetriever
from app.repositories.approval import ApprovalStore
from app.repositories.chat import ChatRepository
from app.repositories.copilot import CopilotEventRepository
from app.repositories.idempotency import IdempotencyStore
from app.repositories.plan import PlanStore
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService


@dataclass
class CopilotRuntime:
    """一次对话请求的运行时装配产物（每请求一实例，工具闭包捕获请求作用域服务）。"""

    # —— 编排消费 ——
    model: BaseChatModel
    model_name: str
    gateway: LLMGateway
    checkpointer: Any
    tracer: BaseCallbackHandler
    runtime: RuntimeConfig
    chat_repository: ChatRepository
    event_repository: CopilotEventRepository
    memory_service: CopilotMemoryService
    memory_store: MemoryFileStore
    skill_store: SkillFileStore
    planner: Planner
    plan_store: PlanStore
    approval_store: ApprovalStore
    reviewer: OutputReviewer
    verifier: SideEffectVerifier
    breaker: CircuitBreaker
    security_breaker: SecurityBreaker

    # —— 装配成品 ——
    context_manager: ContextManager
    tools: list[BaseTool]
    registry: ToolRegistry
    tool_map: dict[str, BaseTool]
    write_tool_names: frozenset[str]
    # 绑好静态原料、只差 tracker/tool_names 的图装配器：graph_builder(tracker, tool_names)
    graph_builder: Callable[..., Any]

    # —— 共享可变状态（原实例属性，函数化后显式收拢）——
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 已加载的自定义 skill 全文（渐进式加载缓存）：get_skill 写、agent 节点读拼 L3。
    invoked_skills: dict[str, str] = field(default_factory=dict)
    # 子 Agent 精简约束（父显式下传）：_assemble_context 每轮写红线 + constraint 硬约束。
    constraints: dict[str, str] = field(default_factory=dict)


def build_runtime(
    *,
    model: BaseChatModel,
    gateway: LLMGateway,
    checkpointer: Any,
    tracer: BaseCallbackHandler,
    rag_retriever: RagRetriever,
    kb_service: KnowledgeBaseService,
    note_service: NoteService,
    document_service: DocumentService,
    web_search: WebSearchClient,
    memory_service: CopilotMemoryService,
    memory_store: MemoryFileStore,
    skill_store: SkillFileStore,
    chat_repository: ChatRepository,
    event_repository: CopilotEventRepository,
    reviewer: OutputReviewer,
    runtime: RuntimeConfig,
    planner: Planner,
    breaker: CircuitBreaker,
    security_breaker: SecurityBreaker,
    verifier: SideEffectVerifier,
    plan_store: PlanStore,
    approval_store: ApprovalStore,
    idempotency_store: IdempotencyStore,
    db_lock: asyncio.Lock,
) -> CopilotRuntime:
    """装配一个 `CopilotRuntime`：工具装配 + 图装配绑定 + 派生缓存，入口函数只收成品。

    所有依赖必填（非 None）：入口是纯装配，不做任何隐式兜底/降级。原「None = 关闭某能力」
    的语义改由调用方（``deps_copilot``）显式提供 concrete 默认（熔断器 / 幂等内存存储 /
    审批内存存储 / no-op 可观测等）——本函数只拼装；任何依赖为 None 即 fail-fast 报错，
    杜绝静默降级（如 checkpointer 悄悄退内存版、verifier 悄悄用 DB 回查）。
    """
    _required = {
        "model": model,
        "gateway": gateway,
        "checkpointer": checkpointer,
        "tracer": tracer,
        "rag_retriever": rag_retriever,
        "kb_service": kb_service,
        "note_service": note_service,
        "document_service": document_service,
        "web_search": web_search,
        "memory_service": memory_service,
        "memory_store": memory_store,
        "skill_store": skill_store,
        "chat_repository": chat_repository,
        "event_repository": event_repository,
        "reviewer": reviewer,
        "runtime": runtime,
        "planner": planner,
        "breaker": breaker,
        "security_breaker": security_breaker,
        "verifier": verifier,
        "plan_store": plan_store,
        "approval_store": approval_store,
        "idempotency_store": idempotency_store,
        "db_lock": db_lock,
    }
    missing = [name for name, value in _required.items() if value is None]
    if missing:
        raise ValueError(f"build_runtime 缺少必填依赖：{', '.join(missing)}")

    # 跨节点共享的可变状态（get_skill 写 / agent 节点读；assemble_context 写 / spawn_rag 读）。
    invoked_skills: dict[str, str] = {}
    constraints: dict[str, str] = {}

    # run 内上下文压缩器（五级渐进）：ratio 超阈值时压缩工具结果/历史，防止注意力稀释。
    # 压缩摘要走网关（统一门禁/记账/快照）——L5 历史压缩唯一归属，编排层不再另设摘要器。
    context_manager = ContextManager(runtime.context, summarizer=gateway)

    # RAG 子 Agent 装配器：绑死 model/reviewer/runtime/... 静态原料，只留 readonly_tools/
    # registry 两个动态参数（与下方 graph_builder 的 partial 同一手法）。tools.py 只按只读
    # 白名单筛工具并调用它实例化，子 Agent 的「怎么装」收敛在本装配层，不再摊在 build_tools。
    rag_subagent_factory = partial(
        RagSubagent,
        model,
        reviewer=reviewer,
        runtime=runtime,
        verifier=verifier,
        breaker=breaker,
        security_breaker=security_breaker,
        review_max_attempts=runtime.review_max_attempts,
        gateway=gateway,
        max_turns=4,  # 子 Agent 最大轮次（原 build_tools.subagent_max_turns 默认值）
    )

    tools, registry = build_tools(
        rag_retriever=rag_retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=web_search,
        memory_service=memory_service,
        memory_store=memory_store,
        skill_store=skill_store,
        max_result_chars=runtime.max_result_chars,
        invoked_skills=invoked_skills,
        constraint_holder=constraints,
        lock=db_lock,
        breaker=breaker,
        idempotency_store=idempotency_store,
        rag_subagent_factory=rag_subagent_factory,
    )
    tool_map = {tool.name: tool for tool in tools}
    write_tool_names = frozenset(name for name, meta in registry.items() if meta.has_side_effect)

    # 图装配器：绑死静态原料，只留 tracker / tool_names 两个每 run 动态参数。
    graph_builder = partial(
        build_reactive_graph,
        model,
        tools,
        gateway=gateway,
        reviewer=reviewer,
        checkpointer=checkpointer,
        review_max_attempts=runtime.review_max_attempts,
        runtime=runtime,
        verifier=verifier,
        registry=registry,
        breaker=breaker,
        security_breaker=security_breaker,
        context_manager=context_manager,
        invoked_skills=invoked_skills,
    )

    return CopilotRuntime(
        model=model,
        model_name=getattr(model, "model_name", "") or "unknown",
        gateway=gateway,
        checkpointer=checkpointer,
        tracer=tracer,
        runtime=runtime,
        chat_repository=chat_repository,
        event_repository=event_repository,
        memory_service=memory_service,
        memory_store=memory_store,
        skill_store=skill_store,
        planner=planner,
        plan_store=plan_store,
        approval_store=approval_store,
        reviewer=reviewer,
        verifier=verifier,
        breaker=breaker,
        security_breaker=security_breaker,
        context_manager=context_manager,
        tools=tools,
        registry=registry,
        tool_map=tool_map,
        write_tool_names=write_tool_names,
        graph_builder=graph_builder,
        db_lock=db_lock,
        invoked_skills=invoked_skills,
        constraints=constraints,
    )
