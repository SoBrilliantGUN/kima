"""LLM 网关：所有 LLM / embedding / rerank 调用的唯一出口（决策 D1/D6/D7）。

设计目标：
- **统一门禁**：每次调用前预检「熔断 + 预算」；预算任一轴超限即硬停（抛
  ``BudgetExceeded``），LLM 服务熔断中快速失败（抛 ``LLMCircuitOpenError``）。
- **统一执行**：瞬时异常退避重试 + 秒轴超时硬熔断。
- **统一记账**：token/cost 回写本 run 的 ``BudgetTracker``（四轴）+ 跨 run 的
  ``DailyBudget``（经 tracker 的 sink 或直接回写），消除「旁路调用漏记账」。
- **调用级快照**（决策 D3/D4）：以「node + 规范化输入」的内容哈希为键，成功后落 output；
  恢复时命中缓存直接复用、不重跑（at-least-once：崩溃窗口内「已成功未落快照」会重放）。

**铁律**：所有 LLM 出口必须经本网关，禁止直接调用 ``LLMClient.chat`` /
``BaseChatModel.ainvoke`` / ``EmbeddingClient.embed_*`` / ``RerankerClient.rerank``。

三套调用形态统一抽象（D6）：
- 形态 A（一次性结构化）：``complete()`` → 包 ``LLMClient.chat``；
- 形态 B（流式工具调用）：``invoke_model()`` → 包 ``BaseChatModel.ainvoke``；
- 形态 C（流式生成）：``stream()`` → 包 ``LLMClient.stream``，逐 token yield 同时缓冲全文记账/快照。

run 期上下文注入（``run_budget``）与序列化/指纹/脱敏纯函数分别见
``gateway_context.py`` / ``gateway_codec.py``。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from app.agent.gateway_codec import (
    aimessage_to_dict,
    canonical_chat,
    canonical_langchain,
    chat_result_to_dict,
    dict_to_aimessage,
    dict_to_chat_result,
    estimate_text_tokens,
    make_call_key,
    redact_chat,
    redact_langchain,
    usage_from_aimessage,
    usage_from_chat_result,
    usage_to_dict,
)
from app.agent.gateway_context import RunContext as RunContext
from app.agent.gateway_context import current
from app.agent.gateway_context import run_budget as run_budget
from app.agent.pricing import PriceQuote, PricingService
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.error_classifier import is_retryable
from app.agent.resilience.retry import RetryPolicy
from app.agent.runtime.budget import DailyBudget, Usage
from app.agent.snapshot import SnapshotStore
from app.core.exceptions import DomainError
from app.integrations.embedding import EmbeddingClient
from app.integrations.llm import ChatMessage, ChatResult, LLMClient
from app.integrations.rerank import RerankerClient, RerankResult
from app.repositories.llm_cost import CostStore

logger = logging.getLogger(__name__)

# 80% 软提示（决策 D2）：任一轴占用 ≥ soft_threshold 时，以「尾三明治」钉到本次输入末尾。
# 该提醒是 volatile 的（随预算状态变化），不参与快照指纹，避免重放时缓存失效。
_SOFT_REMINDER = (
    "（系统提示）本轮预算已用约 {pct:.0%}，接近上限。请立即停止扩展任务，"
    "直接给出你当前能给出的最好答案，不要再发起新的工具调用或尝试更多方案。"
)


def _quote_snapshot(quote: PriceQuote | None) -> dict[str, Any]:
    """把解析到的价格转成成本明细里的 ``price_snapshot``（策略 id + 时段 + 各维度单价）。"""
    if quote is None:
        return {}
    return {
        "policy_id": str(quote.policy_id) if quote.policy_id is not None else None,
        "slot_start": quote.slot_start,
        "slot_end": quote.slot_end,
        "prices": quote.prices,
    }


_T = TypeVar("_T")


class LLMCircuitOpenError(DomainError):
    """LLM 服务熔断中（breaker OPEN）：快速失败，不空转请求。"""

    status_code = 503
    code = "llm_circuit_open"


class UsageMissingError(DomainError):
    """真实厂商调用未返回 token 用量（usage_metadata 缺失/全 0）：账单完整性 fail-closed。

    token/cost 两轴预算与成本审计都依赖 usage；若静默按 0 记账，这两轴会假死、成本记 ¥0，
    失控调用可超预算烧钱。故在真实（非 fake）厂商下用量缺失时直接中止，宁可不放行也不盲记。
    """

    status_code = 502
    code = "usage_missing"


@dataclass(frozen=True)
class GatewayConfig:
    """网关的纯配置旋钮集（无行为对象）。

    与 ``RuntimeConfig`` 同理：原本 timeout/soft_threshold/dlp_redact/各 provider 的
    vendor·model 等标量在构造签名里逐项透传，签名随之膨胀。收敛成一个不可变配置对象，
    一处构造、整体传递；协作对象（llm/breaker/retry/snapshots/embedder/reranker/pricing/
    cost_store/daily_budget）仍按依赖注入单独传入。
    """

    timeout: float = 60.0  # 单次 LLM 调用秒轴硬熔断（默认 60s，恒生效）
    soft_threshold: float = 0.80  # 任一轴占用 ≥ 此值软提示收尾
    dlp_redact: bool = True  # 输入 DLP 脱敏（指纹按脱敏前的原文算）
    llm_vendor: str = ""
    llm_model: str = ""
    embed_vendor: str = ""
    embed_model: str = ""
    rerank_vendor: str = ""
    rerank_model: str = ""


# 默认配置单例：GatewayConfig 是 frozen 不可变，可安全共享；避免在参数默认值里做函数调用（B008）。
_DEFAULT_GATEWAY_CONFIG = GatewayConfig()


class LLMGateway:
    """所有 LLM 调用的统一出口：门禁 → 快照复用 → 重试/超时 → 记账。"""

    def __init__(
        self,
        *,
        config: GatewayConfig = _DEFAULT_GATEWAY_CONFIG,
        llm: LLMClient,
        daily_budget: DailyBudget,
        breaker: CircuitBreaker,
        retry: RetryPolicy,
        snapshots: SnapshotStore,
        embedder: EmbeddingClient,
        reranker: RerankerClient,
        pricing: PricingService,
        cost_store: CostStore,
    ) -> None:
        self._llm = llm
        self._daily_budget = daily_budget
        self._breaker = breaker
        self._retry = retry
        self._snapshots = snapshots
        self._embedder = embedder
        self._reranker = reranker
        self._pricing = pricing
        self._cost_store = cost_store
        self._timeout = config.timeout
        self._soft_threshold = config.soft_threshold
        self._dlp_redact = config.dlp_redact
        self._llm_vendor = config.llm_vendor
        self._llm_model = config.llm_model
        self._embed_vendor = config.embed_vendor
        self._embed_model = config.embed_model
        self._rerank_vendor = config.rerank_vendor
        self._rerank_model = config.rerank_model

    def _vendor_model(self, kind: str) -> tuple[str, str]:
        """按调用类型映射到 (vendor, model)：chat/agent→llm，embedding→embed，rerank→rerank。"""
        if kind == "embedding":
            return self._embed_vendor, self._embed_model
        if kind == "rerank":
            return self._rerank_vendor, self._rerank_model
        return self._llm_vendor, self._llm_model

    def _breaker_key(self, kind: str) -> str:
        """熔断 key 按 ``kind:vendor:model`` 拆分，不同上游服务各自熔断、互不拖累。"""
        vendor, model = self._vendor_model(kind)
        return f"{kind}:{vendor}:{model}"

    # --- 门禁 ---

    def _preflight(self, kind: str) -> None:
        """熔断 + 预算硬停预检；任一不满足即抛异常，不进入调用。"""
        if not self._breaker.is_available(self._breaker_key(kind)):
            raise LLMCircuitOpenError("LLM 服务熔断中，暂时不可用")
        self._preflight_budget()

    def _preflight_budget(self) -> None:
        current().tracker.check()  # 本 run 四轴硬停（抛 BudgetExceeded）
        self._daily_budget.check()  # 跨 run 日上限硬停（抛 BudgetExceeded）

    def _record(self, usage: Usage, *, count_turn: bool, cost_cny: float) -> None:
        """回写用量：run 内经 tracker（内部转 sink 到日预算）；非 run 直接回写日预算。

        ``cost_cny`` 已由 ``_invoke`` 经 ``PricingService`` 算好，本方法只透传（不重算）。
        """
        current().tracker.record(usage, cost_cny=cost_cny, count_turn=count_turn)

    def _soft_reminder(self) -> str | None:
        """任一轴占用 ≥ soft_threshold（且 <100%，硬停已由 preflight 兜住）时返回软提示。"""
        ratio = max(current().tracker.usage_ratio(), self._daily_budget.usage_ratio())
        if ratio >= self._soft_threshold:
            return _SOFT_REMINDER.format(pct=ratio)
        return None

    # --- 执行管线 ---

    async def _account(
        self,
        *,
        call_key: str,
        kind: str,
        usage: Usage,
        count_turn: bool,
        encode_result: dict[str, Any],
    ) -> None:
        """调用成功后的记账/快照/成本明细落库（``_invoke`` 与 ``stream`` 共用）。

        ``encode_result`` 是已编码的结果 payload（供快照落库），``usage`` 是本次调用用量；
        ``count_turn`` 仅 agent 主循环计 turn，辅助/生成调用不计。
        """
        vendor, model = self._vendor_model(kind)
        # fail-closed（账单完整性）：真实厂商（非 fake/空）返回的用量缺失（input/output 双 0）
        # 时立即中止。token/cost 两轴预算与成本审计都依赖 usage，若静默按 0 记账这两轴会假死、
        # 成本记 ¥0，失控调用可超预算烧钱。宁可不放行，也不盲记 0。
        if vendor not in ("", "fake") and usage.input_tokens == 0 and usage.output_tokens == 0:
            raise UsageMissingError(
                f"{kind} 调用（{vendor}/{model}）未返回 token 用量：usage_metadata 缺失或"
                "厂商未上报，"
                "已中止本次调用以保护预算门禁（避免 token/cost 轴静默失效、成本审计记 ¥0）"
            )
        # 计价：成本只在网关算一次（docs/pricing.md 铁律），找不到生效价抛 PricingError fail-closed
        # fake/空厂商不计价（¥0、不落成本明细）：真实厂商才走定价解析与成本审计。
        cost_cny = 0.0
        quote: PriceQuote | None = None
        if vendor not in ("", "fake"):
            quote = await self._pricing.resolve(vendor, model, datetime.now(UTC))
            cost_cny = self._pricing.compute_cost(vendor, usage, quote)
        self._record(usage, count_turn=count_turn, cost_cny=cost_cny)
        self._breaker.record_success(self._breaker_key(kind))
        await self._snapshots.put(
            current().run_id,
            call_key,
            kind=kind,
            output=encode_result,
            usage=usage_to_dict(usage),
        )
        if vendor not in ("", "fake"):
            await self._cost_store.put(
                current().run_id,
                call_key,
                vendor=vendor,
                model=model,
                pricing_policy_id=quote.policy_id if quote is not None else None,
                price_snapshot=_quote_snapshot(quote),
                usage=usage,
                cost_cny=cost_cny,
            )

    async def _invoke(
        self,
        node: str,
        call: Callable[[], Awaitable[_T]],
        *,
        count_turn: bool,
        extract: Callable[[_T], Usage],
        kind: str,
        encode: Callable[[_T], dict[str, Any]],
        decode: Callable[[dict[str, Any]], _T],
        fingerprint: str,
    ) -> _T:
        """五段管线：预检 → 快照复用 → 重试/超时 → 计价记账 → 快照/成本明细落库。"""
        logger.debug("LLM 网关调用 node=%s", node)
        self._preflight(kind)
        run_id = current().run_id
        snapshots = self._snapshots
        call_key = make_call_key(node, fingerprint)
        cached = await snapshots.get(run_id, call_key)
        if cached is not None:
            # 命中：复用已成功调用，不重跑、不计账（纯函数记忆化）
            logger.debug("LLM 快照命中 node=%s", node)
            return decode(cached)

        result = await self._retry_call(call, kind)
        usage = extract(result)
        await self._account(
            call_key=call_key,
            kind=kind,
            usage=usage,
            count_turn=count_turn,
            encode_result=encode(result),
        )
        return result

    async def _retry_call(self, call: Callable[[], Awaitable[_T]], kind: str) -> _T:
        """退避重试 + 秒轴超时硬熔断；瞬时异常重试，永久异常立即上抛。

        每次尝试前重做预算预检：秒轴随时间流逝增长，重试可能把剩余预算耗尽，需在
        「发出下一次请求」前叫停，避免超预算地空烧调用。熔断 key 由 ``kind`` 派生。
        """
        policy = self._retry
        last_exc: BaseException | None = None
        for attempt in range(policy.max_attempts):
            self._preflight(kind)  # 预算超限/熔断在此抛出，不被下方 retry 吞掉
            try:
                # 秒轴硬熔断恒生效：timeout 有非 None 默认值，不允许关掉——否则 LLM 挂起会永久卡死
                return await asyncio.wait_for(call(), timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001 - 分级后决定重试或上抛
                last_exc = exc
                if not is_retryable(exc) or attempt == policy.max_attempts - 1:
                    break
                await asyncio.sleep(policy.delay(attempt + 1))
        if last_exc is not None and is_retryable(last_exc):
            self._breaker.record_failure(self._breaker_key(kind))
        assert last_exc is not None  # for 循环至少执行一次
        raise last_exc

    # --- 三套调用形态 ---

    async def complete(
        self,
        node: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """形态 A：一次性结构化调用（包 ``LLMClient.chat``）。不计 turn。"""

        fingerprint = canonical_chat(messages)
        if self._dlp_redact:
            messages = redact_chat(messages)
        reminder = self._soft_reminder()
        if reminder is not None:
            messages = [*messages, ChatMessage("system", reminder)]

        async def call() -> ChatResult:
            return await self._llm.chat(messages, temperature=temperature, max_tokens=max_tokens)

        return await self._invoke(
            node,
            call,
            count_turn=False,
            extract=usage_from_chat_result,
            kind="chat",
            encode=chat_result_to_dict,
            decode=dict_to_chat_result,
            fingerprint=fingerprint,
        )

    async def invoke_model(
        self,
        node: str,
        model: BaseChatModel,
        messages: list[BaseMessage],
        *,
        count_turn: bool = True,
    ) -> AIMessage:
        """形态 B：流式工具调用（包已 bind_tools 的 ``BaseChatModel.ainvoke``）。

        ``count_turn`` 默认 True（agent 主循环计 turn）；子 Agent 传 False，只计 token/cost、
        不计入主循环 max_turns（子 Agent 有独立 max_turns 兜底）。
        """

        fingerprint = canonical_langchain(messages)
        if self._dlp_redact:
            messages = redact_langchain(messages)
        reminder = self._soft_reminder()
        if reminder is not None:
            messages = [*messages, SystemMessage(content=reminder)]

        async def call() -> AIMessage:
            return await model.ainvoke(messages)

        return await self._invoke(
            node,
            call,
            count_turn=count_turn,
            extract=usage_from_aimessage,
            kind="agent",
            encode=aimessage_to_dict,
            decode=dict_to_aimessage,
            fingerprint=fingerprint,
        )

    async def stream(
        self,
        node: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """形态 C：流式生成（包 ``LLMClient.stream``），逐 token yield 同时缓冲全文供记账/快照。

        ``LLMClient.stream`` 不返回 usage，用量按 ``estimate_text_tokens`` 粗估（与 embed/rerank
        同理）。流式已部分输出无法重试；不注入软提示（最终答案非 agent 循环）。
        """
        fingerprint = canonical_chat(messages)
        if self._dlp_redact:
            messages = redact_chat(messages)
        self._preflight("chat")
        run_id = current().run_id
        call_key = make_call_key(node, fingerprint)
        cached = await self._snapshots.get(run_id, call_key)
        if cached is not None:
            # 命中：复用已成功落盘的全文，作为单个 chunk yield（不重跑、不计账）
            logger.debug("LLM 快照命中 node=%s", node)
            yield dict_to_chat_result(cached).content
            return

        parts: list[str] = []
        try:
            # 秒轴硬熔断包住整个流式消费（LLM 挂起保护，与 _retry_call 的恒生效语义一致）
            async with asyncio.timeout(self._timeout):
                async for delta in self._llm.stream(
                    messages, temperature=temperature, max_tokens=max_tokens
                ):
                    parts.append(delta)
                    yield delta
        except Exception:  # noqa: BLE001 - 流式无重试，记录失败后上抛
            self._breaker.record_failure(self._breaker_key("chat"))
            raise

        text = "".join(parts)
        usage = Usage(
            input_tokens=estimate_text_tokens([m.content for m in messages]),
            output_tokens=estimate_text_tokens([text]),
        )
        await self._account(
            call_key=call_key,
            kind="chat",
            usage=usage,
            count_turn=False,
            encode_result=chat_result_to_dict(ChatResult(content=text)),
        )

    # --- 嵌入 / 精排（决策 D1：模块 5 也纳入网关） ---

    async def embed(self, node: str, texts: list[str]) -> list[list[float]]:
        """嵌入文档向量（包 ``EmbeddingClient.embed_documents``）。不计 turn。"""
        if not texts:
            return []
        est = estimate_text_tokens(texts)

        async def call() -> list[list[float]]:
            return await self._embedder.embed_documents(texts)

        return await self._invoke(
            node,
            call,
            count_turn=False,
            extract=lambda _r: Usage(input_tokens=est),
            kind="embedding",
            encode=lambda vectors: {"vectors": vectors},
            decode=lambda data: list(data["vectors"]),
            fingerprint=json.dumps(texts, ensure_ascii=False),
        )

    async def embed_query(self, node: str, text: str) -> list[float]:
        """单条查询向量（包 ``EmbeddingClient.embed_query``）。不计 turn。"""
        est = estimate_text_tokens([text])

        async def call() -> list[float]:
            return await self._embedder.embed_query(text)

        return await self._invoke(
            node,
            call,
            count_turn=False,
            extract=lambda _r: Usage(input_tokens=est),
            kind="embedding",
            encode=lambda vector: {"vector": vector},
            decode=lambda data: list(data["vector"]),
            fingerprint=json.dumps([text], ensure_ascii=False),
        )

    async def rerank(self, node: str, query: str, documents: list[str]) -> list[RerankResult]:
        """精排（包 ``RerankerClient.rerank``）。不计 turn。"""
        if not documents:
            return []
        est = estimate_text_tokens([query, *documents])

        async def call() -> list[RerankResult]:
            return await self._reranker.rerank(query, documents)

        return await self._invoke(
            node,
            call,
            count_turn=False,
            extract=lambda _r: Usage(input_tokens=est),
            kind="rerank",
            encode=lambda results: {
                "results": [{"index": r.index, "score": r.score} for r in results]
            },
            decode=lambda data: [
                RerankResult(index=d["index"], score=d["score"]) for d in data["results"]
            ],
            fingerprint=json.dumps({"q": query, "docs": documents}, ensure_ascii=False),
        )
