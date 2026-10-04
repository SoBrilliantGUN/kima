import asyncio
import math
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.gateway import GatewayConfig, LLMGateway, run_budget
from app.agent.guardrail.review import (
    OutputReviewer,
    ReviewResult,
    ReviewVerdict,
    SideEffectVerifier,
)
from app.agent.memory_classifier import MemoryClassification
from app.agent.pricing import PriceQuote, PricingService
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.retry import Backoff, RetryPolicy
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.budget import BudgetTracker, DailyBudget, HardBudget, Usage
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.plan_model import Plan, PlanStep
from app.agent.runtime.reactive import build_reactive_graph
from app.agent.side_effect import DbSideEffectVerifier
from app.agent.snapshot import InMemorySnapshotStore, SnapshotStore
from app.agent.toolmeta import ToolRegistry
from app.integrations.embedding import EmbeddingClient, FakeEmbeddingClient
from app.integrations.llm import ChatMessage, ChatResult, LLMClient
from app.integrations.parser import ParsedDocument, ParserError, SourceType
from app.integrations.rerank import FakeRerankerClient, RerankerClient
from app.models.copilot import CopilotEvent, CopilotMemory, MemoryKind
from app.models.document import MAX_RETRIES, Document, DocumentChunk, DocumentStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.note import Note
from app.repositories.approval import InMemoryApprovalStore
from app.repositories.idempotency import InMemoryIdempotencyStore
from app.repositories.llm_cost import CostStore, InMemoryCostStore
from app.repositories.pricing import InMemoryPricingRepository
from app.services.conflict import ConflictVerdict
from app.services.copilot import CopilotMemoryService
from app.services.note import NoteService


class FakeKnowledgeBaseRepository:
    """内存版 repository，模拟 SQLAlchemy 实现的插入语义（分配 id + 时间戳）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, KnowledgeBase] = {}

    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]:
        items = sorted(
            self._store.values(),
            key=lambda kb: (kb.updated_at, kb.id),
            reverse=True,
        )
        return items[offset : offset + limit], len(items)

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self._store.get(kb_id)

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        for kb in self._store.values():
            if kb.name == name:
                return kb
        return None

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        kb.id = uuid.uuid4()
        now = datetime.now(UTC)
        kb.created_at = now
        kb.updated_at = now
        self._store[kb.id] = kb
        return kb

    async def update(self, kb: KnowledgeBase) -> KnowledgeBase:
        kb.updated_at = datetime.now(UTC)
        self._store[kb.id] = kb
        return kb

    async def delete(self, kb: KnowledgeBase) -> None:
        self._store.pop(kb.id, None)


class FakeNoteRepository:
    """内存版笔记 repository，模拟 SQLAlchemy 实现（分配 id + 时间戳 + 关联幂等）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Note] = {}
        self._associations: set[tuple[uuid.UUID, uuid.UUID]] = set()

    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]:
        items = sorted(
            self._store.values(),
            key=lambda note: (note.updated_at, note.id),
            reverse=True,
        )
        return items[offset : offset + limit], len(items)

    async def get(self, note_id: uuid.UUID) -> Note | None:
        return self._store.get(note_id)

    async def add(self, note: Note) -> Note:
        note.id = uuid.uuid4()
        now = datetime.now(UTC)
        note.created_at = now
        note.updated_at = now
        self._store[note.id] = note
        return note

    async def update(self, note: Note) -> Note:
        note.updated_at = datetime.now(UTC)
        self._store[note.id] = note
        return note

    async def delete(self, note: Note) -> None:
        self._store.pop(note.id, None)
        self._associations = {(n, k) for (n, k) in self._associations if n != note.id}

    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool:
        key = (note_id, kb_id)
        if key in self._associations:
            return False
        self._associations.add(key)
        return True

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Note]:
        note_ids = {n for (n, k) in self._associations if k == kb_id}
        notes = [self._store[n] for n in note_ids if n in self._store]
        return sorted(notes, key=lambda note: (note.updated_at, note.id), reverse=True)

    async def get_by_content_hash(self, content_hash: str) -> Note | None:
        for note in self._store.values():
            if note.content_hash == content_hash:
                return note
        return None

    async def create_unique(self, note: Note) -> Note | None:
        """模拟唯一索引：content_hash 已存在（且非空）则返回 None，否则落库。"""
        for existing in self._store.values():
            if existing.content_hash is not None and existing.content_hash == note.content_hash:
                return None
        await self.add(note)
        return note


class FakeDocumentRepository:
    """内存版文档 repository，模拟 SQLAlchemy 实现（分配 id/时间戳 + 父子 chunk 存储）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Document] = {}
        self._chunks: dict[uuid.UUID, list[DocumentChunk]] = {}

    async def add(self, doc: Document) -> Document:
        doc.id = uuid.uuid4()
        now = datetime.now(UTC)
        doc.created_at = now
        doc.updated_at = now
        if doc.status is None:
            doc.status = DocumentStatus.PENDING
        if doc.retry_count is None:
            doc.retry_count = 0
        self._store[doc.id] = doc
        return doc

    async def get(self, doc_id: uuid.UUID) -> Document | None:
        return self._store.get(doc_id)

    async def update(self, doc: Document) -> Document:
        doc.updated_at = datetime.now(UTC)
        self._store[doc.id] = doc
        return doc

    async def delete(self, doc: Document) -> None:
        self._store.pop(doc.id, None)
        self._chunks.pop(doc.id, None)

    async def claim_pending(self, limit: int) -> list[Document]:
        now = datetime.now(UTC)
        pending = [
            d
            for d in self._store.values()
            if d.status == DocumentStatus.PENDING
            and (d.next_retry_at is None or d.next_retry_at <= now)
        ]
        pending.sort(key=lambda d: d.created_at)
        claimed = pending[:limit]
        for doc in claimed:
            doc.status = DocumentStatus.PROCESSING
            doc.error_message = None
        return claimed

    async def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        for chunk in chunks:
            self._chunks.setdefault(chunk.document_id, []).append(chunk)

    async def delete_chunks(self, doc_id: uuid.UUID) -> None:
        self._chunks.pop(doc_id, None)

    async def recover_stuck(self, now: datetime, max_age: timedelta) -> int:
        cutoff = now - max_age
        stuck = [
            d
            for d in self._store.values()
            if d.status == DocumentStatus.PROCESSING and d.updated_at <= cutoff
        ]
        for doc in stuck:
            doc.retry_count += 1
            if doc.retry_count > MAX_RETRIES:
                doc.status = DocumentStatus.ERROR
                doc.next_retry_at = None
            else:
                doc.status = DocumentStatus.PENDING
                doc.next_retry_at = now
            doc.error_message = "处理超时，已重置"
        return len(stuck)

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Document]:
        docs = [d for d in self._store.values() if d.kb_id == kb_id]
        return sorted(docs, key=lambda d: (d.updated_at, d.id), reverse=True)

    async def close(self) -> None:
        """内存版无连接池，close 为空操作（对齐 SQLAlchemy 实现的会话关闭）。"""

    # 测试辅助：直接读取/注入，避开 async 语义
    def chunks_of(self, doc_id: uuid.UUID) -> list[DocumentChunk]:
        return self._chunks.get(doc_id, [])


class FakeFileStore:
    """内存文件存储，记录保存/删除供断言。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def save(self, content: bytes, ext: str) -> str:
        name = f"{uuid.uuid4().hex}.{ext}"
        self.files[name] = content
        return name

    async def open(self, path: str) -> bytes:
        return self.files[path]

    async def delete(self, path: str) -> None:
        if path in self.files:
            self.files.pop(path)
            self.deleted.append(path)


class FakeDailyBudgetStore:
    """内存版日预算 store：``add`` 原子累加增量（对齐 DB 的 ON CONFLICT 语义）。"""

    def __init__(self) -> None:
        self._rows: dict[date, tuple[float, int]] = {}

    async def load(self, day: date) -> tuple[float, int] | None:
        return self._rows.get(day)

    async def add(self, day: date, cost_cny: float, tokens: int) -> None:
        prev = self._rows.get(day)
        if prev is None:
            self._rows[day] = (cost_cny, tokens)
        else:
            self._rows[day] = (prev[0] + cost_cny, prev[1] + tokens)


class FakeDocumentParser:
    """假分发器（满足 DocumentParser 胖签名），ingest/worker 注入用。"""

    def __init__(
        self,
        markdown: str = "# 解析正文",
        title: str = "解析标题",
        *,
        fail: bool = False,
    ) -> None:
        self.markdown = markdown
        self.title = title
        self.fail = fail
        self.calls: list[str] = []

    async def parse(
        self,
        *,
        source_type: SourceType,
        content: bytes | None = None,
        url: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        self.calls.append(source_type.value)
        if self.fail:
            raise ParserError("解析失败")
        return ParsedDocument(markdown=self.markdown, title=self.title, metadata={})


class FakeFileParser:
    """窄文件解析器替身（同时满足 PdfParser / WordParser），记录调用并返回固定 markdown。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def parse(self, *, content: bytes, filename: str | None = None) -> ParsedDocument:
        self.calls.append(self.name)
        return ParsedDocument(markdown=f"# {self.name}")


class FakeUrlParser:
    """窄 URL 解析器替身（满足 UrlParser），记录调用并返回固定 markdown。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def parse(self, *, url: str) -> ParsedDocument:
        self.calls.append(self.name)
        return ParsedDocument(markdown=f"# {self.name}")


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """两向量余弦距离（1 - 余弦相似度），FakeCopilotMemoryRepository.search 用。"""
    if not a or not b:
        return 1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


class FakeCopilotMemoryRepository:
    """内存版记忆仓库：分配 id/时间戳，search 按余弦距离排序（对齐 pgvector 语义）。"""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, CopilotMemory] = {}

    async def add(self, memory: CopilotMemory) -> CopilotMemory:
        memory.id = uuid.uuid4()
        now = datetime.now(UTC)
        if memory.created_at is None:
            memory.created_at = now
        if memory.updated_at is None:
            memory.updated_at = now
        # 模拟列默认值（SQLAlchemy 在 INSERT 时才会应用 default=0，直接构造时为 None）
        if memory.access_count is None:
            memory.access_count = 0
        self._store[memory.id] = memory
        return memory

    async def get(self, memory_id: uuid.UUID) -> CopilotMemory | None:
        return self._store.get(memory_id)

    async def get_many(self, memory_ids: list[uuid.UUID]) -> list[CopilotMemory]:
        return [self._store[mid] for mid in memory_ids if mid in self._store]

    async def update(self, memory: CopilotMemory) -> CopilotMemory:
        memory.updated_at = datetime.now(UTC)
        self._store[memory.id] = memory
        return memory

    async def delete_superseded_older_than(self, cutoff: datetime) -> int:
        doomed = [
            m
            for m in self._store.values()
            if m.superseded and m.superseded_at is not None and m.superseded_at < cutoff
        ]
        for memory in doomed:
            self._store.pop(memory.id, None)
        return len(doomed)

    async def search(
        self, kind: MemoryKind, query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]:
        candidates = [
            m
            for m in self._store.values()
            if m.kind == kind and not m.superseded and m.embedding is not None
        ]
        candidates.sort(key=lambda m: _cosine_distance(m.embedding or [], query_vec))
        return candidates[:top_k]

    async def search_lexical(self, kind: MemoryKind, query: str, top_k: int) -> list[CopilotMemory]:
        """词法召回模拟：子串命中（单测无 pg_jieba，用「query 出现在 content 里」近似 BM25）。"""
        candidates = [
            m
            for m in self._store.values()
            if m.kind == kind and not m.superseded and query in m.content
        ]
        return candidates[:top_k]

    async def search_cross_kind(
        self, kinds: list[MemoryKind], query_vec: list[float], top_k: int
    ) -> list[CopilotMemory]:
        candidates = [
            m
            for m in self._store.values()
            if m.kind in kinds and not m.superseded and m.embedding is not None
        ]
        candidates.sort(key=lambda m: _cosine_distance(m.embedding or [], query_vec))
        return candidates[:top_k]

    async def search_recoverable(
        self,
        kinds: list[MemoryKind],
        query_vec: list[float],
        similarity_threshold: float,
        now: datetime,
        window: timedelta,
        top_k: int,
    ) -> list[CopilotMemory]:
        cutoff = now - window
        candidates = [
            m
            for m in self._store.values()
            if m.kind in kinds
            and m.superseded
            and m.superseded_at is not None
            and m.superseded_at >= cutoff
            and m.embedding is not None
            and _cosine_distance(m.embedding or [], query_vec) <= 1.0 - similarity_threshold
        ]
        candidates.sort(key=lambda m: _cosine_distance(m.embedding or [], query_vec))
        return candidates[:top_k]

    async def list_active(self, kind: MemoryKind) -> list[CopilotMemory]:
        return [m for m in self._store.values() if m.kind == kind and not m.superseded]

    async def get_by_entity(self, kind: MemoryKind, entity_id: str) -> CopilotMemory | None:
        matches = [
            m
            for m in self._store.values()
            if m.kind == kind and m.entity_id == entity_id and not m.superseded
        ]
        return max(matches, key=lambda m: m.version) if matches else None

    async def overwrite_entity(
        self,
        memory_id: uuid.UUID,
        content: str,
        embedding: list[float],
        trigger_conditions: dict[str, Any] | None,
    ) -> CopilotMemory:
        memory = self._store.get(memory_id)
        assert memory is not None
        memory.content = content
        memory.embedding = embedding
        memory.trigger_conditions = trigger_conditions
        memory.version += 1
        memory.updated_at = datetime.now(UTC)
        return memory

    async def count_active(self, kind: MemoryKind) -> int:
        return len([m for m in self._store.values() if m.kind == kind and not m.superseded])

    async def touch(self, memory_ids: list[uuid.UUID], now: datetime) -> None:
        for memory_id in memory_ids:
            memory = self._store.get(memory_id)
            if memory is not None:
                memory.access_count += 1
                memory.last_access = now


class FakeCopilotEventRepository:
    """内存版事件日志仓库：自增 seq，记录调用供断言。"""

    def __init__(self) -> None:
        self.events: list[CopilotEvent] = []
        self._seq = 0

    async def add_event(self, event: CopilotEvent) -> CopilotEvent:
        self._seq += 1
        event.seq = self._seq
        event.id = uuid.uuid4()
        event.created_at = datetime.now(UTC)
        self.events.append(event)
        return event

    async def list_events(self, run_id: uuid.UUID) -> list[CopilotEvent]:
        return [e for e in self.events if e.run_id == run_id]


class FakeConflictJudge:
    """确定性冲突判定：按给定 verdicts 逐条回放（不足补 none），记录调用。"""

    def __init__(self, verdicts: list[str] | None = None) -> None:
        self._verdicts = verdicts or []
        self.calls: list[tuple[str, list[str]]] = []

    async def judge(self, new_content: str, candidates: list[str]) -> list[ConflictVerdict]:
        self.calls.append((new_content, candidates))
        verdicts = list(self._verdicts)
        verdicts = verdicts + ["none"] * (len(candidates) - len(verdicts))
        return [ConflictVerdict(v) for v in verdicts[: len(candidates)]]


class FakeMemoryClassifier:
    """确定性分类器：按给定分类逐次回放（无结果回退 None），记录调用。"""

    def __init__(self, classifications: list[MemoryClassification | None] | None = None) -> None:
        self._classifications = list(classifications or [])
        self.calls: list[str] = []

    async def classify(self, raw: str) -> MemoryClassification | None:
        self.calls.append(raw)
        if self._classifications:
            return self._classifications.pop(0)
        return None


class FakeOutputReviewer:
    """脚本化输出审查器：按给定 results 逐次回放（不足回退 ok），记录调用。"""

    def __init__(self, results: list[ReviewResult] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[str, str]] = []

    async def review(self, final_answer: str, trace: str) -> ReviewResult:
        self.calls.append((final_answer, trace))
        if self._results:
            return self._results.pop(0)
        return ReviewResult(verdict=ReviewVerdict.OK, issues=[])


class FakePlanner:
    """no-op 规划器：generate 返回空 Plan（退化为 reactive），replan 返回空步骤。

    build_runtime 要求 planner 恒在场（非 None），多数测试不触发 PLAN 意图、不真正
    调用规划器，故用空实现占位即可；需要真规划器行为的用例另注入 LLMPlanner。
    """

    async def generate(
        self, task: str, tool_names: list[str], constraints: str = "", skills: str = ""
    ) -> Plan:
        return Plan(steps=())

    async def replan(
        self,
        plan: Plan,
        failed_step: PlanStep,
        error: str,
        tool_names: Sequence[str],
        skills: str = "",
    ) -> list[PlanStep]:
        return []


class ScriptedLLM:
    """按序回放内容的自定义 LLMClient（测试自纠错循环：先错后对）。

    ``prompt_tokens`` / ``completion_tokens`` 可选，逐次对齐 ``contents``，用于测试
    预算记账（不足时回退 0）。
    """

    def __init__(
        self,
        contents: list[str],
        prompt_tokens: list[int] | None = None,
        completion_tokens: list[int] | None = None,
    ) -> None:
        self._contents = list(contents)
        self._prompt_tokens = list(prompt_tokens or [])
        self._completion_tokens = list(completion_tokens or [])
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.calls.append(messages)
        content = self._contents.pop(0) if self._contents else ""
        prompt = self._prompt_tokens.pop(0) if self._prompt_tokens else 0
        completion = self._completion_tokens.pop(0) if self._completion_tokens else 0
        return ChatResult(content=content, prompt_tokens=prompt, completion_tokens=completion)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self._contents:
            yield self._contents.pop(0)


class FakePricingService(PricingService):
    """测试替身：任意厂商 resolve 返回 ¥0 全时段报价，compute_cost 恒 0（不碰 DB/strategy）。

    网关只在真实厂商（非 fake/空）下走定价；真实厂商用例（如 deepseek）经本替身直接 ¥0 通过，
    避免每个用例都去 seed 价格目录。
    """

    def __init__(self) -> None:
        super().__init__(InMemoryPricingRepository(), cache_ttl_seconds=0)

    async def resolve(self, vendor: str, model: str, now: datetime) -> PriceQuote:
        return PriceQuote(policy_id=None, slot_start="00:00", slot_end="24:00", prices={})

    def compute_cost(self, vendor: str, usage: Usage, quote: PriceQuote) -> float:
        return 0.0


def make_copilot_defaults(
    note_service: NoteService,
    memory_service: CopilotMemoryService,
    verifier: SideEffectVerifier | None = None,
) -> dict[str, Any]:
    """构造 ``build_runtime`` 所需「可关闭能力」的 concrete 默认（恒在场，非 None）。

    build_runtime 要求所有依赖必填：熔断器 / 安全熔断 / 幂等内存存储 / 计划检查点 /
    审批内存存储 / 内存 checkpointer / no-op 可观测 / no-op 规划器，这里统一给默认。
    db_lock 与 verifier 共享同一把锁；verifier 默认用 DB 回查实现，可注入 Fake 覆盖。
    """
    db_lock = asyncio.Lock()
    return {
        "db_lock": db_lock,
        "verifier": verifier or DbSideEffectVerifier(note_service, memory_service, db_lock),
        "checkpointer": InMemorySaver(),
        "tracer": BaseCallbackHandler(),
        "planner": FakePlanner(),
        "breaker": CircuitBreaker(),
        "security_breaker": SecurityBreaker(),
        "approval_store": InMemoryApprovalStore(),
        "idempotency_store": InMemoryIdempotencyStore(),
    }


def make_gateway(
    *,
    config: GatewayConfig | None = None,
    llm: LLMClient | None = None,
    daily_budget: DailyBudget | None = None,
    breaker: CircuitBreaker | None = None,
    retry: RetryPolicy | None = None,
    snapshots: SnapshotStore | None = None,
    embedder: EmbeddingClient | None = None,
    reranker: RerankerClient | None = None,
    pricing: PricingService | None = None,
    cost_store: CostStore | None = None,
) -> LLMGateway:
    """构造一个全协作者就位的 ``LLMGateway``（默认全 fake/no-op），可按需覆盖任一协作者。

    网关要求所有协作者非空；测试统一经本工厂装配默认 fake，避免每个用例重复传一堆无关依赖，
    也避免网关构造签名变化时逐个改用例。
    """
    return LLMGateway(
        config=config or GatewayConfig(),
        llm=llm or ScriptedLLM(["ok"]),
        daily_budget=daily_budget
        or DailyBudget(max_cost_cny=1e9, max_tokens=10**12, store=FakeDailyBudgetStore()),
        breaker=breaker or CircuitBreaker(failure_threshold=10_000),
        retry=retry or RetryPolicy(max_attempts=1, base_delay=0.0, backoff=Backoff.FIXED),
        snapshots=snapshots or InMemorySnapshotStore(),
        embedder=embedder or FakeEmbeddingClient(dimension=8),
        reranker=reranker or FakeRerankerClient(),
        pricing=pricing or FakePricingService(),
        cost_store=cost_store or InMemoryCostStore(),
    )


class FakeSideEffectVerifier:
    """no-op 副作用对账器：恒返回 None（不强制 mismatch）。

    需要失败场景的用例另注入 _FailingVerifier。
    """

    async def verify(self, tool_name: str, args: dict[str, Any], result: str) -> str | None:
        return None


def make_runtime_config(**overrides: Any) -> RuntimeConfig:
    """构造带 concrete 默认的 ``RuntimeConfig``（daily_budget 必填），可按需覆盖任意旋钮。"""
    kwargs: dict[str, Any] = {
        "daily_budget": DailyBudget(
            max_cost_cny=1e9, max_tokens=10**12, store=FakeDailyBudgetStore()
        ),
    }
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


def make_reactive_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    reviewer: OutputReviewer | None = None,
    checkpointer: Any = None,
    runtime: RuntimeConfig | None = None,
    verifier: SideEffectVerifier | None = None,
    registry: ToolRegistry | None = None,
    breaker: CircuitBreaker | None = None,
    security_breaker: SecurityBreaker | None = None,
    gateway: LLMGateway | None = None,
    tracker: BudgetTracker | None = None,
    **kwargs: Any,
) -> Any:
    """测试 helper：构造 reactive 图，为恒在场参数提供 concrete 默认，可按需覆盖。

    与 ``build_reactive_graph`` 不同，本 helper 的 Optional 参数用 None 表示「用默认 fake」，
    而非「关闭能力」——避免每个用例手写一串无关依赖。
    """
    runtime = runtime or make_runtime_config()
    reviewer = reviewer or FakeOutputReviewer()
    verifier = verifier if verifier is not None else FakeSideEffectVerifier()
    registry = registry if registry is not None else {}
    breaker = breaker or CircuitBreaker()
    security_breaker = security_breaker or SecurityBreaker()
    gateway = gateway or make_gateway()
    checkpointer = checkpointer or InMemorySaver()
    tracker = tracker or BudgetTracker(runtime.budget, sink=runtime.daily_budget)
    return build_reactive_graph(
        model,
        tools,
        reviewer=reviewer,
        checkpointer=checkpointer,
        runtime=runtime,
        verifier=verifier,
        registry=registry,
        breaker=breaker,
        security_breaker=security_breaker,
        gateway=gateway,
        tracker=tracker,
        **kwargs,
    )


@asynccontextmanager
async def gateway_run(run_id: str = "r1") -> AsyncIterator[None]:
    """测试辅助：给一次/多次网关调用提供最小 run 上下文（无上限 tracker + run_id）。

    网关只能在 run_budget 里跑，直接调 LLM 后端组件（planner/classifier/reviewer/
    summarizer/judge）前用它包裹，模拟「处于一次 copilot run 中」。
    """
    with run_budget(BudgetTracker(HardBudget()), run_id=run_id):
        yield
