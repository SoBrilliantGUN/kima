"""单次 agent run 的流式累加器。

替代 `_stream_graph` 的一串可变 out-param：此前 answer_parts / all_steps / call_name_by_id /
review_step_holder / behavior_tracker / trust_holder 六个容器被塞进方法签名、边流边写、
流结束后再读回拼最终回答。收敛成一个 `RunSession`，方法签名从 10 参降到 5 参。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.guardrail.trust import BehaviorTracker
from app.agent.runtime.context import RunState


@dataclass
class RunSession:
    """收集一次 graph 流的 delta 文本 / 工具轨迹 / review 步骤。

    `_stream_graph` 边流边写入本对象；流结束后调用方先 `flush_review` 收尾，再取
    `answer_parts` / `all_steps` 拼最终回答。可信度不再汇聚成本对象的一个字段——
    它已下沉为各数据块的 ``<data trust=...>`` 标记。

    `run_state` 是编排层生命周期（running→suspended/completed/failed），由 `stream_graph`
    在三个真实边界写入（interrupt/review 判终/异常），收尾时透传给 `commit_assistant` 落
    done 事件——生命周期状态放在编排层循环持有的可变对象（本对象）而非图内。
    """

    write_tool_names: frozenset[str]
    answer_parts: list[str] = field(default_factory=list)
    all_steps: list[dict[str, Any]] = field(default_factory=list)
    call_name_by_id: dict[str, str] = field(default_factory=dict)
    review_step: dict[str, Any] | None = None
    run_state: str = RunState.RUNNING.value
    behavior_tracker: BehaviorTracker = field(init=False)

    def __post_init__(self) -> None:
        self.behavior_tracker = BehaviorTracker(write_tool_names=self.write_tool_names)

    def flush_review(self) -> None:
        """把 review 步骤并入工具轨迹（review 事件在流结束后才最终确定）。"""
        if self.review_step is not None:
            self.all_steps.append(self.review_step)
            self.review_step = None
