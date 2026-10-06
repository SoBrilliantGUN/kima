"""CopilotService 的共享小工具：预算包裹 / 文本裁剪 / 来源评级。

从 service.py 抽出，供 reactive 主流程、planner/qa 两种执行模式复用，避免这些
无状态的纯函数拖累编排层。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.runtime.budget import BudgetExceeded, BudgetTracker, Usage
from app.agent.toolmeta import ToolRegistry

# 工具结果落事件日志的截断长度（防超长结果撑爆 observability 存储）
TOOL_RESULT_CHARS = 2000


def tool_source(name: str, registry: ToolRegistry | None) -> str:
    """工具结果来源评级：kb / web / tool_result（零信任打分用，来自 ToolMeta）。"""
    if registry is not None:
        meta = registry.get(name)
        if meta is not None:
            return meta.source
    return "tool_result"


async def bounded_call(
    awaitable: Awaitable[Any],
    tracker: BudgetTracker | None,
    usage_fn: Callable[[Any], Usage],
    *,
    label: str,
) -> Any:
    """预算包裹：进前 check、`asyncio.wait_for` 硬熔断、进后 record（planner/qa 复用）。

    `usage_fn` 在结果返回后据其计算本次 token 用量（LLMClient 用 prompt/completion、
    BaseChatModel 用 usage_metadata、纯工具步骤恒 Usage()）。
    """
    if tracker is None:
        return await awaitable
    tracker.check()
    timeout = tracker.remaining_seconds()
    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise BudgetExceeded(f"seconds 超限（{label} {timeout:.1f}s 未返回）") from exc
    # 这里只包「非 LLM 的 awaitable」（工具调用/检索，Usage()=0）或 gateway=None 的降级
    # LLM 兜底——两种都不计价（成本 0），真实 LLM 计价走网关（docs/pricing.md 铁律）。
    tracker.record(usage_fn(result), cost_cny=0.0)
    return result


def clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def extract_llm_text(raw: Any) -> str:
    """从工具返回值提取给 LLM 的文本。

    约定：工具返回 dict 时，``summary`` 字段是给 LLM 的可读文本（其余字段给程序 / for /
    参数链读）；list 逐元素提取（for 聚合结果）；其他类型 ``str()`` 兜底。
    """
    if isinstance(raw, dict):
        return str(raw.get("summary") or "")
    if isinstance(raw, list):
        return "\n".join(extract_llm_text(item) for item in raw)
    return str(raw)
