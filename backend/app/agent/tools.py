"""Copilot 工具集（16 个）：副作用分层（11 只读 / 5 写）+ 六要素描述 + 元数据注册。

工具闭包捕获请求作用域的仓储/服务（每个请求独立 session），故在 `build_tools` 内定义。
每个工具用 ``@copilot_tool`` 一个装饰器同时声明「四层执行守卫（熔断 → 重试 → 超时 →
串行化）」与「ToolMeta 安全指纹」（副作用等级 / 来源评级 / 超时 / 幂等 / 上下行契约），
并在 docstring 里用六要素框架描述（用途/区别/参数/约束/示例），让模型能选对工具。

``@copilot_tool`` 自动收集被装饰的工具，`build_tools` 结束时据此派生 ``(tools, registry)``：
registry 是「工具名 → ToolMeta」的单一真源（name 取自函数名，不再手写），Loop 的
并发裁决 / 权限门禁 / 零信任打分 / 幂等键注入都从它派生。工具定义处即注册处，
加工具不再需要改第二处（无 ``tools`` 列表、无 ``tools_registry`` 字典）。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agent.helpers import clip
from app.agent.resilience.circuit_breaker import CircuitBreaker, with_circuit_breaker
from app.agent.resilience.result import ToolFailure, ToolOutcome
from app.agent.resilience.retry import DEFAULT_RETRY_POLICY, with_retry
from app.agent.resilience.spill import SpillStore
from app.agent.resilience.timeout import with_timeout
from app.agent.toolmeta import (
    MAX_VISIBLE_TOOLS,
    SYNTHESIS_RESULT_CHARS,
    OutputContract,
    ParamContract,
    SideEffectLevel,
    ToolMeta,
    ToolRegistry,
    request_hash_for,
)
from app.agent.tools_helpers import (
    DOC_ID_CONTRACT,
    LIST_LIMIT_DEFAULT,
    LIST_PARAM_CONTRACT,
    MEMORY_KIND_CONTRACT,
    NOTE_ID_CONTRACT,
    PROFILE_KIND_CONTRACT,
    WEB_TOP_K,
    Fn,
    parse_kb_ids,
    parse_kind,
    parse_uuid,
    serialize,
    timeout_s,
    with_has_more,
)
from app.core.exceptions import DomainError
from app.core.memory_store import MemoryFileStore
from app.core.skill_store import SkillFileStore
from app.integrations.search import WebSearchClient
from app.rag.retriever import RagRetriever
from app.repositories.idempotency import (
    IdempotencyOutcome,
    IdempotencyStore,
)
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService

# 检索类工具共用上行契约：结果流向 synthesizer（父 Agent）前截到结论级长度，
# 长正文仍可经 spill / read_tool_result 按需取回（「大体积数据只传引用」）。
_RETRIEVAL_CONTRACT = OutputContract(max_chars=SYNTHESIS_RESULT_CHARS)

# RAG 子 Agent 的候选工具集（只读检索，独立窗口多步循环，只回结论）。
_RAG_SUBAGENT_TOOL_NAMES = frozenset(
    {"search_knowledge_base", "read_document", "read_note", "search_web", "search_memory"}
)

# QA 主循环工具白名单（公开，service 层 QA 路由用）：只读检索五件套 + spawn_rag。
# QA 可派子 Agent 下沉重检索，但不写、不递归（spawn_rag 不在 _RAG_SUBAGENT_TOOL_NAMES 里）。
QA_TOOL_NAMES = _RAG_SUBAGENT_TOOL_NAMES | {"spawn_rag"}


def build_tools(
    *,
    rag_retriever: RagRetriever,
    kb_service: KnowledgeBaseService,
    note_service: NoteService,
    document_service: DocumentService,
    web_search: WebSearchClient,
    memory_service: CopilotMemoryService,
    memory_store: MemoryFileStore,
    skill_store: SkillFileStore,
    max_result_chars: int,
    invoked_skills: dict[str, str] | None = None,
    constraint_holder: dict[str, str] | None = None,
    lock: asyncio.Lock,
    spill_store: SpillStore | None = None,
    breaker: CircuitBreaker,
    idempotency_store: IdempotencyStore,
    rag_subagent_factory: Callable[[list[BaseTool], ToolRegistry], Any],
) -> tuple[list[BaseTool], ToolRegistry]:
    """构建工具集 + 元数据注册表（闭包捕获请求作用域服务）。

    `lock` 用于串行化共享 AsyncSession 的访问，须与 CopilotService 的 DB 操作共用同一把。
    `idempotency_store` 是写工具幂等去重的持久化后端（Stripe 式幂等表，见
    ``repositories/idempotency.py``）；恒在场（无 None 降级，内存版由调用方显式提供）。
    `rag_subagent_factory` 是 RAG 子 Agent 的装配器（``readonly_tools, registry → RagSubagent``），
    由装配层（``compose.build_runtime``）以 partial 绑死 model/reviewer/runtime 等静态原料后
    注入；此处只按只读白名单筛工具并实例化，子 Agent 的「怎么装」不落在本模块。
    返回 ``(tools, registry)``：registry 是「工具名 → ToolMeta」的单一真源，由
    ``@copilot_tool`` 装饰器自动收集派生，Loop 据此裁决。
    """

    spill_store = spill_store or SpillStore()
    idempotency = idempotency_store
    invoked_skills = invoked_skills if invoked_skills is not None else {}

    # 装饰器自动收集（定义处即注册处），build_tools 结束时据此派生 tools + registry。
    collected: list[tuple[BaseTool, ToolMeta]] = []

    def _guarded(latency_ms: int, resource: str | None = None) -> Callable[[Fn], Fn]:
        """四层守卫：熔断 → 重试 → 超时 → 串行化（由 `@copilot_tool` 先套，再包 `@tool`）。

        ``resource`` 声明工具所属共享依赖（级联熔断）：失败/成功同时记到 ``resource:<name>``，
        一个工具反复失败拖累同资源的所有工具（DB 挂 → 所有 db 工具一起熔断）。
        """

        def decorator(fn: Fn) -> Fn:
            return with_circuit_breaker(breaker, resource=resource)(
                with_retry(DEFAULT_RETRY_POLICY)(
                    with_timeout(timeout_s(latency_ms))(serialize(lock)(fn))
                )
            )

        return decorator

    def copilot_tool(
        *,
        hint: str,
        side_effect_level: SideEffectLevel,
        source: str,
        latency_ms: int,
        enforced_idempotent: bool = False,
        idempotency_key_fields: tuple[str, ...] = (),
        tier: str = "l1",
        output_contract: OutputContract | None = None,
        param_contract: ParamContract | None = None,
        resource: str | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]]], BaseTool]:
        """工具即契约：一个装饰器同时挂「执行守卫」与「ToolMeta 安全指纹」，并自动收集。

        ``latency_ms`` / ``resource`` 一份声明、两处派生——既喂执行守卫（超时上限 =
        latency_ms × 3、级联熔断），又进 ToolMeta（``estimated_latency_ms`` / ``resource``），
        不再手写两遍。``name`` 从 ``@tool`` 产出的 ``BaseTool.name`` 自动取（= 函数名），
        消掉「字典 key 与 ToolMeta.name 各写一遍」的漂移。
        """

        def decorator(fn: Callable[..., Awaitable[Any]]) -> BaseTool:
            t = tool(_guarded(latency_ms, resource)(fn))
            collected.append(
                (
                    t,
                    ToolMeta(
                        name=t.name,
                        hint=hint,
                        side_effect_level=side_effect_level,
                        source=source,
                        estimated_latency_ms=latency_ms,
                        enforced_idempotent=enforced_idempotent,
                        idempotency_key_fields=idempotency_key_fields,
                        tier=tier,
                        output_contract=output_contract,
                        param_contract=param_contract,
                        resource=resource,
                    ),
                )
            )
            return t

        return decorator

    def _finalize(text: str, tool_name: str) -> str:
        """结果超 `max_result_chars` 时落盘（spill）而非截断，返回占位符。"""
        if len(text) <= max_result_chars:
            return text
        return spill_store.spill(text, tool_name)

    def _deny(reason: str, hint: str, code: str) -> ToolFailure:
        """红灯：永久失败，告知模型别重试、换策略。"""
        return ToolFailure(outcome=ToolOutcome.PERMANENT, reason=reason, hint=hint, code=code)

    async def _dedup(tool_name: str, idem_key: str, request_hash: str) -> str | None:
        """写工具幂等去重（执行契约）：claim 抢占，命中缓存/处理中返回直接结果，冲突/永久失败
        抛红灯。

        返回 None 表示抢到执行权、应继续执行业务；返回字符串表示应直接返回该结果。
        """
        claim = await idempotency.claim(tool_name, idem_key, request_hash)
        if claim.outcome is IdempotencyOutcome.INSERTED:
            return None
        if claim.outcome is IdempotencyOutcome.CACHED:
            return claim.response or ""
        if claim.outcome is IdempotencyOutcome.PROCESSING:
            return "该操作正在处理中，请稍后重试。"
        if claim.outcome is IdempotencyOutcome.CONFLICT:
            raise _deny(
                "同一幂等键下参数不同，拒绝执行（请勿复用旧键）",
                "重试时不要更改参数，或用不同内容重新执行。",
                "idempotency_conflict",
            )
        # FAILED_FINAL
        raise _deny(
            claim.error or "该操作此前已永久失败",
            "此前相同操作已永久失败，请检查参数后换一种方式。",
            "idempotency_failed_final",
        )

    # --- 只读：检索 / 列出 / 读取 ---

    @copilot_tool(
        hint="检索知识库原文",
        side_effect_level=SideEffectLevel.LOW,
        source="kb",
        latency_ms=3000,
        output_contract=_RETRIEVAL_CONTRACT,
        resource="db",
    )
    async def search_knowledge_base(query: str, kb_ids: list[str] | None = None) -> str:
        """检索知识库内的文档/笔记原文。
        【用途】当需要基于知识库资料回答、或查找库内某主题内容时使用。
        【区别】搜的是「已入库原文」，语义+关键词混合检索；查外部信息用 search_web，
        查个人长期记忆用 search_memory。
        【参数】query 为检索问题；kb_ids 可选知识库 id 列表，不传则检索全部知识库。
        【约束】只读无副作用；返回片段可能很长，超限会落盘，用 read_tool_result 查全文。
        【示例】search_knowledge_base("项目架构", ["<kb_id>"]) → 带编号命中片段（含标题与正文）。"""
        ids = await parse_kb_ids(kb_ids, kb_service)
        if not ids:
            return "知识库为空，无可检索内容。"
        chunks = await rag_retriever.retrieve(query, ids)
        if not chunks:
            return "未检索到相关内容。"
        body = "\n\n".join(f"[{i + 1}] {c.title}\n{c.content}" for i, c in enumerate(chunks))
        return _finalize(body, "search_knowledge_base")

    @copilot_tool(
        hint="列出知识库",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=500,
        param_contract=LIST_PARAM_CONTRACT,
        resource="db",
    )
    async def list_knowledge_bases(limit: int = LIST_LIMIT_DEFAULT, offset: int = 0) -> str:
        """列出全部知识库。
        【用途】需要知道有哪些知识库、或拿到知识库 id 时使用。
        【区别】只列知识库元信息（id/名称/描述）；列笔记用 list_notes，
        检索内容用 search_knowledge_base。
        【参数】limit 每页条数（默认 50，上限 100）；offset 偏移量。
        【约束】只读无副作用；条数超过一页会提示继续列。
        【示例】list_knowledge_bases() → 每行「id | 名称 | 描述」。"""
        items, total = await kb_service.list(limit=limit, offset=offset)
        if not items:
            return "暂无知识库。"
        lines = [f"{kb.id} | {kb.name} | {kb.description or ''}" for kb in items]
        return _finalize(with_has_more(lines, offset, total, "知识库"), "list_knowledge_bases")

    @copilot_tool(
        hint="列出笔记",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=500,
        param_contract=LIST_PARAM_CONTRACT,
        resource="db",
    )
    async def list_notes(limit: int = LIST_LIMIT_DEFAULT, offset: int = 0) -> str:
        """列出全部笔记。
        【用途】需要知道有哪些笔记、或拿到笔记 id 时使用。
        【区别】只列笔记元信息（id/标题）；列知识库用 list_knowledge_bases，
        检索内容用 search_knowledge_base。
        【参数】limit 每页条数（默认 50，上限 100）；offset 偏移量。
        【约束】只读无副作用；条数超过一页会提示继续列。
        【示例】list_notes() → 每行「id | 标题」。"""
        items, total = await note_service.list(limit=limit, offset=offset)
        if not items:
            return "暂无笔记。"
        lines = [f"{note.id} | {note.title}" for note in items]
        return _finalize(with_has_more(lines, offset, total, "笔记"), "list_notes")

    @copilot_tool(
        hint="读文档全文",
        side_effect_level=SideEffectLevel.LOW,
        source="kb",
        latency_ms=1500,
        output_contract=_RETRIEVAL_CONTRACT,
        param_contract=DOC_ID_CONTRACT,
        resource="db",
    )
    async def read_document(document_id: str) -> str:
        """读取指定文档的完整正文（markdown）。
        【用途】需要细读某篇已入库文档内容时使用。
        【区别】读的是「文档原文」；读笔记用 read_note。
        【参数】document_id 为文档 id（先用 list_knowledge_bases / search_knowledge_base 拿 id）。
        【约束】只读无副作用；id 非法会永久失败（不要重试同一 id）。
        【示例】read_document("<doc_id>") → 文档标题 + markdown 正文。"""
        doc_id = parse_uuid(document_id)
        assert doc_id is not None  # DOC_ID_CONTRACT 已保证合法，此处不可达
        try:
            document = await document_service.get(doc_id)
            content = await document_service.get_content(doc_id)
            return _finalize(f"# {document.title}\n\n{content}", "read_document")
        except DomainError as exc:
            raise _deny(
                str(exc), "该文档不存在，请先用 search_knowledge_base 找到存在的 id。", "not_found"
            ) from exc

    @copilot_tool(
        hint="读笔记全文",
        side_effect_level=SideEffectLevel.LOW,
        source="kb",
        latency_ms=500,
        output_contract=_RETRIEVAL_CONTRACT,
        param_contract=NOTE_ID_CONTRACT,
        resource="db",
    )
    async def read_note(note_id: str) -> str:
        """读取指定笔记的完整正文（markdown）。
        【用途】需要细读某篇笔记内容时使用。
        【区别】读的是「笔记原文」；读文档用 read_document。
        【参数】note_id 为笔记 id（先用 list_notes 拿 id）。
        【约束】只读无副作用；id 非法会永久失败（不要重试同一 id）。
        【示例】read_note("<note_id>") → 笔记标题 + markdown 正文。"""
        nid = parse_uuid(note_id)
        assert nid is not None  # NOTE_ID_CONTRACT 已保证合法，此处不可达
        try:
            note = await note_service.get(nid)
            return _finalize(f"# {note.title}\n\n{note.content_markdown}", "read_note")
        except DomainError as exc:
            raise _deny(
                str(exc), "该笔记不存在，请先用 list_notes 找到存在的 id。", "not_found"
            ) from exc

    @copilot_tool(
        hint="联网搜索",
        side_effect_level=SideEffectLevel.LOW,
        source="web",
        latency_ms=5000,
        output_contract=_RETRIEVAL_CONTRACT,
        resource="web",
    )
    async def search_web(query: str) -> str:
        """联网搜索。
        【用途】问题超出知识库范围、需要最新信息或外部资料时使用。
        【区别】搜公网信息；搜知识库用 search_knowledge_base，搜个人长期记忆用 search_memory。
        【参数】query 为搜索词。
        【约束】只读无副作用；返回带标题/链接/摘要。
        【示例】search_web("2026 年最新 React 版本") → 搜索结果。"""
        results = await web_search.search(query, top_k=WEB_TOP_K)
        if not results:
            return "未搜到结果。"
        body = "\n\n".join(f"{r.title}\n{r.url}\n{r.snippet}" for r in results)
        return _finalize(body, "search_web")

    @copilot_tool(
        hint="检索长期记忆",
        side_effect_level=SideEffectLevel.LOW,
        source="kb",
        latency_ms=2000,
        output_contract=_RETRIEVAL_CONTRACT,
        param_contract=MEMORY_KIND_CONTRACT,
        resource="db",
    )
    async def search_memory(query: str, kind: str | None = None) -> str:
        """检索长期记忆。
        【用途】需要回忆之前记下的用户约束/偏好/事实/事件时使用。
        【区别】搜个人长期记忆；搜知识库用 search_knowledge_base，搜公网用 search_web。
        【参数】query 为检索问题；kind 可选 constraint/fact/preference/episodic，
        不传则事实+情节都查。
        【约束】只读无副作用；kind 非法会永久失败。
        【示例】search_memory("用户喜欢什么", "fact") → 命中记忆条目。"""
        k = parse_kind(kind)
        hits = await memory_service.search_memory(query, k)
        if not hits:
            return "未找到相关记忆。"
        return _finalize(
            "\n".join(f"- [{m.kind.value}] {m.content}" for m in hits), "search_memory"
        )

    @copilot_tool(
        hint="读落盘结果全文",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=100,
    )
    async def read_tool_result(path: str, grep_pattern: str | None = None) -> str:
        """读取之前落盘（spill）的工具结果全文。
        【用途】当某工具返回「结果已落盘」占位符、需要查看完整内容时使用。
        【区别】读的是「已落盘的临时结果」；读文档原文用 read_document，读笔记原文用 read_note。
        【参数】path 为占位符里的文件名；grep_pattern 可选，命中则返回含该子串的行。
        【约束】只读无副作用；path 无效会返回「未找到」，不会重试；返回内容有长度上限，
        超限截断，可用 grep_pattern 缩小范围。
        【示例】read_tool_result("<path>") → 落盘全文。"""
        content = spill_store.read(path)
        if content is None:
            return "未找到该落盘结果，path 无效。"
        if grep_pattern:
            lines = [ln for ln in content.splitlines() if grep_pattern in ln]
            content = "\n".join(lines) or "（无匹配行）"
        # 资源契约（硬上限）：read_tool_result 是唯一可返回无界正文的工具——落盘全文/宽 grep
        # 结果可能极大，直接灌回模型会撑爆上下文（文档「画面三」）。这里截断而非 _finalize：
        # 对 spill 结果再 spill 会无限套娃；超限提示用更精确的 grep_pattern 缩小范围。
        if len(content) > max_result_chars:
            content = (
                clip(content, max_result_chars)
                + "\n（结果过长已截断，请用 grep_pattern 缩小范围后重查）"
            )
        return content

    # --- 写：create_note / write_memory / update_profile（副作用在描述里声明） ---

    @copilot_tool(
        hint="新建笔记",
        side_effect_level=SideEffectLevel.MEDIUM,
        source="tool_result",
        latency_ms=500,
        enforced_idempotent=True,
        idempotency_key_fields=("content",),
        resource="db",
    )
    async def create_note(
        title: str, content: str, kb_id: str | None = None, idempotency_key: str | None = None
    ) -> str:
        """新建一篇笔记。
        【用途】当用户要求「总结成笔记」「写一份报告/文档」等产出成文内容时使用。
        【区别】产出持久化成文内容（笔记），是唯一会建笔记的写工具；写长期记忆用 write_memory，
        改档案/人设用 update_profile。
        【参数】title 为标题；content 为 markdown 正文；kb_id 可选（指定则同时挂到该知识库）。
        【约束】写操作（会真的建笔记）；同正文内容去重（幂等），不会重复建。
        【示例】create_note("会议纪要", "# 纪要\n...") → 已创建笔记 <id>。"""
        if idempotency_key:
            cached = await _dedup(
                "create_note",
                idempotency_key,
                request_hash_for({"title": title, "content": content, "kb_id": kb_id}),
            )
            if cached is not None:
                return cached
        try:
            note = await note_service.create_with_content(title, content, parse_uuid(kb_id))
        except DomainError as exc:
            if idempotency_key:
                await idempotency.fail("create_note", idempotency_key, str(exc))
            raise _deny(str(exc), "请检查知识库 id 是否合法后重试。", "create_failed") from exc
        result = f"已创建笔记 {note.id}（标题：{note.title}）"
        if idempotency_key:
            await idempotency.succeed("create_note", idempotency_key, result)
        return result

    @copilot_tool(
        hint="写长期记忆",
        side_effect_level=SideEffectLevel.MEDIUM,
        source="tool_result",
        latency_ms=5000,
        enforced_idempotent=True,
        idempotency_key_fields=("kind", "content", "entity_id"),
        param_contract=MEMORY_KIND_CONTRACT,
        resource="db",
    )
    async def write_memory(
        kind: str, content: str, entity_id: str | None = None, idempotency_key: str | None = None
    ) -> str:
        """写一条长期积累记忆。
        【用途】当对话出现值得长期记住的用户约束/偏好/事实/事件时使用。
        【区别】写积累型记忆（走冲突判定去重）；产出一篇成文笔记用 create_note，
        写人设/档案用 update_profile。
        【参数】kind 为 constraint（硬规则/红线）/fact（事实）/preference（偏好）/
        episodic（事件）；content 为记忆内容；entity_id 可选（fact 的稳定实体键，
        同实体覆盖）。服务端写入前会确定性分类兜底，kind 可能被纠正。
        【约束】写操作（会真的写入记忆库）；fact 同 entity_id 覆盖、其余走冲突判定。
        【示例】write_memory("constraint", "禁止泄露用户隐私数据") → 已写入记忆。"""
        if idempotency_key:
            cached = await _dedup(
                "write_memory",
                idempotency_key,
                request_hash_for({"kind": kind, "content": content, "entity_id": entity_id}),
            )
            if cached is not None:
                return cached
        k = parse_kind(kind)
        assert k is not None  # MEMORY_KIND_CONTRACT 已保证合法，此处不可达
        memory = await memory_service.write_memory(k, content, entity_id)
        result = f"已写入 {kind} 记忆 {memory.id}。"
        if idempotency_key:
            await idempotency.succeed("write_memory", idempotency_key, result)
        return result

    @copilot_tool(
        hint="更新档案/人设",
        side_effect_level=SideEffectLevel.HIGH,
        source="tool_result",
        latency_ms=100,
        param_contract=PROFILE_KIND_CONTRACT,
        resource="file",
    )
    async def update_profile(kind: str, content: str) -> str:
        """更新个人档案 / 人设文件（soul 或 user）。
        【用途】当用户明确要求记住「我的偏好/背景」或「AI 的说话风格/人设」时使用。
        【区别】覆盖写稳定的 profile 文件；产出一篇成文笔记用 create_note，
        写长期积累记忆用 write_memory。
        【参数】kind 为 soul 或 user；content 为要覆盖写入的内容。
        【约束】写操作（会覆盖对应文件）；覆盖写天然幂等。
        【示例】update_profile("user", "用户是后端工程师") → 已更新 user 档案。"""
        await memory_store.write(kind, content)
        return f"已更新 {kind} 档案。"

    @copilot_tool(
        hint="列出 skill",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=100,
        param_contract=LIST_PARAM_CONTRACT,
        resource="file",
    )
    async def list_skills(limit: int = LIST_LIMIT_DEFAULT, offset: int = 0) -> str:
        """列出已安装的自定义 skill（经验技巧）。
        【用途】需要查看有哪些可复用的经验/技巧时使用。
        【区别】列出自定义 skill（沉淀的「怎么做」经验）；官方内置工具由系统注入、无需列出。
        【参数】limit 每页条数（默认 50，上限 100）；offset 偏移量。
        【约束】只读无副作用；条数超过一页会提示继续列。
        【示例】list_skills() → 每行「name：description」。"""
        skills, total = await skill_store.list_skills(limit, offset)
        if not skills:
            return "（无自定义 skill）"
        lines = [f"- {s.name}：{s.description or '（无描述）'}" for s in skills]
        return _finalize(with_has_more(lines, offset, total, "skill"), "list_skills")

    @copilot_tool(
        hint="加载 skill 到会话",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=100,
        resource="file",
    )
    async def get_skill(name: str) -> str:
        """加载某个自定义 skill 的全文到本会话（渐进式加载，跨轮次持续生效）。
        【用途】看到系统提示里的「可用 Skills」列表后，需要应用某个经验/技巧时先加载它。
        【区别】get_skill 把全文加载进 L3（跨轮次持久，后续每轮持续注入）。
        【参数】name 为 skill 名（「可用 Skills」列表里有）。
        【约束】只读无副作用；名字不存在会失败（不要重试同一名字）。
        【示例】get_skill("写周报") → 已加载 skill「写周报」，后续持续遵守其指引。"""
        skill = await skill_store.read_skill(name)
        if skill is None:
            raise _deny(
                f"skill「{name}」不存在", "请先用 list_skills 拿到存在的名字。", "not_found"
            )
        invoked_skills[name] = skill.content
        return f"已加载 skill「{name}」，本会话后续会持续遵守其指引。"

    @copilot_tool(
        hint="写/覆盖 skill",
        side_effect_level=SideEffectLevel.MEDIUM,
        source="tool_result",
        latency_ms=100,
        resource="file",
    )
    async def write_skill(name: str, description: str, content: str) -> str:
        """创建或覆盖一个自定义 skill（经验技巧）。
        【用途】对话中出现值得沉淀的「怎么做」经验/技巧时，把它写成一个 skill。
        【区别】写 skill 是「怎么做」的可复用经验；写偏好/事实/事件用 write_memory。
        【参数】name 为 skill 名；description 一句话说明；content 为正文（markdown）。
        【约束】写操作（会覆盖同名 skill）；覆盖写天然幂等。
        【示例】write_skill("写周报", "写周报用这个模板", "1. 本周完成 ...") → 已写入 skill。"""
        await skill_store.write_skill(name, description, content)
        return f"已写入 skill「{name}」。"

    @copilot_tool(
        hint="删除 skill",
        side_effect_level=SideEffectLevel.HIGH,
        source="tool_result",
        latency_ms=100,
        resource="file",
    )
    async def delete_skill(name: str) -> str:
        """删除一个自定义 skill（只能删用户安装的 skill，不可逆，需用户确认）。
        【用途】用户明确要求删除某个经验/技巧时使用。
        【区别】只能删自定义 skill；官方内置工具不可删。
        【参数】name 为 skill 名（先用 list_skills 拿名字）。
        【约束】高危写操作（删了不可恢复，会触发审批确认）；名字不存在会失败。
        【示例】delete_skill("写周报") → 已删除 skill。"""
        deleted = await skill_store.delete_skill(name)
        if not deleted:
            raise _deny(
                f"skill「{name}」不存在", "请先用 list_skills 拿到存在的名字。", "not_found"
            )
        return f"已删除 skill「{name}」。"

    # RAG 子 Agent（SubAgent）：复用主循环图（对等完整版），独立窗口检索、只回结论。
    # 装配原料（model/reviewer/runtime/...）由调用方经 ``rag_subagent_factory`` 提供
    # （见 compose.build_runtime 的 partial），此处只按只读白名单筛工具并实例化。
    readonly_tools = [
        t for t, meta in collected if meta.is_readonly and t.name in _RAG_SUBAGENT_TOOL_NAMES
    ]
    readonly_registry = {
        t.name: meta
        for t, meta in collected
        if meta.is_readonly and t.name in _RAG_SUBAGENT_TOOL_NAMES
    }
    rag_subagent = rag_subagent_factory(readonly_tools, readonly_registry)

    @copilot_tool(
        hint="派检索子 Agent，只回结论",
        side_effect_level=SideEffectLevel.LOW,
        source="tool_result",
        latency_ms=30000,
    )
    async def spawn_rag(task: str) -> str:
        """派一个检索子 Agent 检索知识库，只返回结论（保护主 Agent 上下文）。
        【用途】需要检索知识库并综合成结论时使用，避免把大段原文塞进主对话。
        【区别】spawn_rag 派子 Agent 独立检索、只回结论；直接检索用 search_knowledge_base。
        【参数】task 为检索子任务描述（含目标与约束）。
        【约束】只读无副作用；子 Agent 独立窗口、多轮检索，延迟较高。
        【示例】spawn_rag("检索项目架构并总结要点") → 结论文本。"""
        constraints = (constraint_holder or {}).get("constraints", "")
        return await rag_subagent.run(task, constraints=constraints)

    tools = [t for t, _ in collected]
    registry: ToolRegistry = {t.name: meta for t, meta in collected}
    if len(tools) > MAX_VISIBLE_TOOLS:
        raise ValueError(
            f"工具数 {len(tools)} 超过单次推理可见上限 {MAX_VISIBLE_TOOLS}："
            "模型会因注意力稀释而降智。请按 L1/L2/L3 分层懒加载（toolmeta.tier）或拆分工具，"
            "而不是让候选集超载。"
        )
    return tools, registry
