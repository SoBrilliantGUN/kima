"""LangFuse 可观测：网关层手动 span 埋点（对齐 docs/module-6-copilot.md §8）。

从「langchain callback」（只吃 LangChain 调用）改成「网关层显式 observation」——
网关三种调用形态（complete / invoke_model / stream / embed / rerank）统一在
``LLMGateway`` 处上报，覆盖所有 LLM / embedding / rerank 调用。

设计要点：
- **start/end 模型**：``start()`` 在调用前建 observation、``end()`` 在调用后收尾，
  duration 由 LangFuse OTel span 自动计时（无需手动传延迟）。
- **session 聚合**：每个网关调用 = 一条 observation，``session_id = run_id``，
  LangFuse UI 按 session 聚合一个 run 的全部调用。不搞嵌套 trace 树——async 下
  OTel context 传播有已知断裂问题，且对「看成本/延迟/错误」无必要。
- **无 key 默认关**：``get_observability`` 在 provider 非 cloud 或缺 key 时返回
  ``NoopObservability``（纯空实现，根本不实例化 LangFuse SDK），不阻塞业务。
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from app.core.config import Settings


class Observation(Protocol):
    """一次可观测调用的生命周期句柄（start 后必须 end）。"""

    def end(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_cny: float | None = None,
        error: str | None = None,
        cache_hit: bool = False,
    ) -> None: ...


class Observability(Protocol):
    """可观测 sink：网关每次 LLM/embedding/rerank 调用上报一条 observation。

    start/end 模型（duration 由后端自动计时）。无 key 时用 ``NoopObservability``。
    """

    def start(
        self,
        *,
        node: str,
        kind: str,
        vendor: str,
        model: str,
        run_id: str,
    ) -> Observation: ...

    def flush(self) -> None: ...


class NoopObservation:
    """无 key 时的空句柄：``end`` 什么都不做。"""

    def end(self, **kwargs: Any) -> None:
        return None


class NoopObservability:
    """无 key 时的空 sink：不实例化 LangFuse SDK，纯 no-op。"""

    def start(self, **kwargs: Any) -> Observation:
        return NoopObservation()

    def flush(self) -> None:
        return None


# 网关 kind → LangFuse observation 类型：LLM 调用（chat/agent）→ generation，
# embedding → embedding，其余（rerank 等）→ span。
_GENERATION_KINDS = frozenset({"chat", "agent"})
_EMBEDDING_KINDS = frozenset({"embedding"})


def _as_type(kind: str) -> str:
    if kind in _GENERATION_KINDS:
        return "generation"
    if kind in _EMBEDDING_KINDS:
        return "embedding"
    return "span"


class _LangfuseObservation:
    """``start_observation`` 返回对象的适配：``end`` 时把结果/错误写入并结束。"""

    def __init__(self, obs: Any, *, kind: str, vendor: str, model: str) -> None:
        self._obs = obs
        self._metadata = {"kind": kind, "vendor": vendor, "model": model, "currency": "CNY"}

    def end(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_cny: float | None = None,
        error: str | None = None,
        cache_hit: bool = False,
    ) -> None:
        if error is not None:
            self._obs.update(level="ERROR", status_message=error, metadata=self._metadata)
        elif cache_hit:
            self._obs.update(metadata={**self._metadata, "cache_hit": True})
        else:
            usage: dict[str, int] = {}
            if input_tokens is not None:
                usage["input"] = input_tokens
            if output_tokens is not None:
                usage["output"] = output_tokens
            cost: dict[str, float] = {}
            if cost_cny is not None:
                cost["total"] = cost_cny
            self._obs.update(
                usage_details=usage or None,
                cost_details=cost or None,
                metadata=self._metadata,
            )
        self._obs.end()


class LangfuseObservability:
    """LangFuse 实现：``start`` 建 observation，``end`` 写 usage/cost/错误并结束。

    LangFuse v3 基于 OTel：``start_observation`` 返回的是同步 span，``end()`` 同步
    结束，上报走 SDK 后台批量线程（flush_interval 默认 5s）。因此 start/end 夹住
    async 网关调用是安全的（start/end 本身不阻塞事件循环）。
    """

    def __init__(self, settings: Settings) -> None:
        from langfuse import Langfuse  # 延迟导入：只在配了 key 时才加载 SDK

        self._client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_host,
        )

    def start(
        self, *, node: str, kind: str, vendor: str, model: str, run_id: str
    ) -> Observation:
        obs = self._client.start_observation(
            name=node,
            as_type=cast(Any, _as_type(kind)),
            model=model or None,
        )
        obs.update_trace(session_id=run_id)
        return _LangfuseObservation(obs, kind=kind, vendor=vendor, model=model)

    def flush(self) -> None:
        self._client.flush()


def get_observability(settings: Settings) -> Observability:
    """按 settings 返回可观测 sink；provider 非 cloud 或缺 key 时返回 no-op（关闭）。"""
    if settings.langfuse_provider != "cloud":
        return NoopObservability()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return NoopObservability()
    return LangfuseObservability(settings)
