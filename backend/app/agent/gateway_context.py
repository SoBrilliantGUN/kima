"""LLM 网关的 run 期上下文注入（ContextVar）。

本 run 的 ``BudgetTracker`` 是 per-run 闭包对象（``build_reactive_graph`` 内创建），
而网关是 app 级单例——故用 ``ContextVar`` 在 run 期间注入「当前 tracker + run_id」。

**铁律**：网关只能在 RunContext 里跑。``run_budget`` 的 ``tracker``/``run_id`` 都必填：
- ``run_id`` 是快照划界 / 成本明细归因 / 幂等键的键；
- ``tracker`` 是本 run 的四轴预算累计器（并回写跨 run 日预算）。

没有 context 就调用网关是配置错误——``current()`` 直接 fail-fast，宁可炸也不静默跳过快照
与记账（「快照/记账不可关」）。worker/RAG（ingest/note_vectorize 向量化、检索）同样要
先 ``run_budget`` 再调网关，把每一次 embed/rerank 都算进账。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.agent.runtime.budget import BudgetTracker


@dataclass(frozen=True)
class RunContext:
    """一次 run 期间注入网关的上下文：本 run 四轴 tracker + thread_id（两者都必填）。"""

    run_id: str
    tracker: BudgetTracker


# 当前 run 的上下文（per-run 注入；未进入 run_budget 时为 None，网关调用即 fail-fast）
_current_run: ContextVar[RunContext | None] = ContextVar("llm_gateway_run", default=None)


def current() -> RunContext:
    """取当前 run 上下文；未进入 ``run_budget`` 则抛错（网关不允许在无 context 下调用）。"""
    ctx = _current_run.get()
    if ctx is None:
        raise RuntimeError(
            "LLM 网关调用必须在 run_budget 上下文中（缺 tracker/run_id）；"
            "请先以 run_budget(tracker, run_id=...) 包裹，否则无法记账/快照"
        )
    return ctx


@contextmanager
def run_budget(tracker: BudgetTracker, *, run_id: str) -> Iterator[None]:
    """在 run 期间把 ``tracker``/``run_id`` 注入网关 contextvar；退出时恢复原值。

    ``tracker``/``run_id`` 都必填。service 层在 ``graph.astream`` 前用本上下文包裹，使
    run 内所有网关调用都能命中当前 run 的四轴预算与调用级快照；run 结束后自动清空。
    """
    token = _current_run.set(RunContext(run_id=run_id, tracker=tracker))
    try:
        yield
    finally:
        _current_run.reset(token)
