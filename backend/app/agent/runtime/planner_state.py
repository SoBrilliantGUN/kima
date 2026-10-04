"""planner 执行图的状态 schema（可序列化供 checkpoint 恢复）。

与 reactive 的 ``AgentState`` 不同，planner 图不靠 ``messages`` 驱动，而是靠 ``plan``
（Plan-as-Data 的版本化 DAG）驱动：supervisor 读 plan 状态算 ready、worker 执行单个 step
回写 results/failures、finalize 挑 terminal 合成产物产出 final_answer、review 读
trace+final_answer 对账。

``plan`` 用 dict（``Plan.to_dict()``）：可变对象会破坏 checkpoint 的值语义，dict 天然
JSON 可序列化、与事件溯源同源（见 docs/planner-supervisor-refactor.md §3）。
"""

from operator import add
from typing import Annotated, Any, TypedDict


def _merge_dict(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """dict 合并 reducer：并行 worker 各自的 results/failures 累加，而非后者覆盖前者。"""
    return {**old, **new}


class PlannerState(TypedDict):
    plan: dict[str, Any]  # Plan.to_dict()，节点边界 from_dict/to_dict 转换
    task: str  # 用户任务
    system_prompt: str  # L0（合成步骤共用）
    memory_block: str  # L2 召回约束（合成步骤共用）
    skills_block: str  # 召回的相关 skill 全文块（planner 写入、合成共用；checkpoint 持久化）
    results: Annotated[dict[str, str], _merge_dict]  # step_id → output_ref（裁剪后，供合成）
    failures: Annotated[dict[str, str], _merge_dict]  # step_id → 错误文本
    trace: Annotated[list[dict[str, Any]], add]  # 统一执行轨迹，元素 {"tool","args","result","ok"}
    completed_steps: Annotated[list[dict[str, Any]], add]  # 完成的 step 元数据（事件流用）
    final_answer: str  # finalize 挑 terminal 合成产物、review 可能追加更正后缀
    plan_error: str  # replan 失败终止的错误信息（非空则合成输出失败）
    step_id: str  # dispatch 经 Send 传给 worker 的当前 step（覆盖写，worker 执行时读）
    last_error: dict[str, Any] | None  # 崩溃现场（与 AgentState 同字段语义）
