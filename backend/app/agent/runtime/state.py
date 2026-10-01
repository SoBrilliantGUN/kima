"""Agent 运行时状态（reactive 循环的状态 schema，可序列化供 checkpoint 恢复）。

六层上下文窗口（见 ``docs/context-window-layering.md``）里，L0/L2/L4 是**固定层文本**（run 开始时
注入、不参与 ``messages`` 累加、永不压缩），L1 是**状态快照来源**（轻量运行时标量），L5 才是
``messages``（history + 工具日志，唯一压缩对象）。agent 节点每次调用模型时，把 L0/L2/L4/L1
临时拼到 L5 之前，再送模型——固定层不进 checkpoint，保 Prompt Cache 前缀稳定 + 压缩只打 L5。
"""

from operator import add
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """reactive 图状态：messages 累加（add_messages），其余字段覆盖写。

    六层上下文窗口字段：
    - ``system_prompt``（L0）/ ``memory_block``（L2）/ ``reminder``（L4）：
      run 开始时注入的固定文本。
    - ``run_state`` / ``turn_count`` / ``tool_failures`` / ``last_action``：
      L1 状态快照来源（agent/tool/review 节点回写）。

    原字段：``attempts`` / ``review_verdict`` / ``review_issues`` / ``correction``
    由 review 节点写入，供条件边路由与 service 层发 SSE 事件；``compression_level``
    是 run 内上下文压缩级别（0-4，见 context.py），由 compress 节点写入；``last_error``
    是崩溃现场/判决书（含分类标签）。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    system_prompt: str  # L0：宪法 + soul/user + 操作引导
    memory_block: str  # L2：[MEMORY] 召回记忆块
    reminder: str  # L4：[REMINDER] 宪法尾部重放
    run_state: str  # L1 快照：run 生命周期（RunState 值，running/completed）
    turn_count: int  # L1 快照：轮次计数
    tool_failures: int  # L1 快照：工具失败次数
    last_action: str  # L1 快照：最后一次动作
    # review 发现不一致时的自动修复重试次数（初始 0，mismatch 分支累加），review 节点写入
    attempts: int
    # review 判决（ok/repaired/corrected/unverified/mismatch），供条件边路由（mismatch → 回 agent）
    review_verdict: str
    review_issues: list[dict[str, str]]  # review 发现的问题清单，供 service 层发 SSE 事件
    correction: str  # 诚实更正提示，仅 review 在 corrected/unverified 分支写入，拼进最终回答
    compression_level: int  # run 内上下文压缩级别（0=无压缩），compress 节点写入
    # 崩溃现场：{"tool","message","kind","fingerprint"}，kind ∈ transient/permanent
    # （经 format_last_error 注入 [STATE] 快照；fingerprint 供永久失败拦截）
    last_error: dict[str, Any] | None
    # 统一执行轨迹（累加，供 review 对账），元素 {"tool","args","result","ok"}
    trace: Annotated[list[dict[str, Any]], add]
    final_answer: str  # 最后一次 agent 回答文本，review 对账用
