"""Copilot 流式事件类型：run 过程中经 SSE 推给前端的结构化事件。

每个事件是 frozen dataclass，`CopilotStreamEvent` 是全部事件的联合类型。事件本身无
行为、无编排依赖，独立成模块以便 service / routes / 测试复用而无需引入服务依赖。
"""

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CopilotMetaEvent:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID


@dataclass(frozen=True)
class CopilotStepEvent:
    tool_name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class CopilotDeltaEvent:
    text: str


@dataclass(frozen=True)
class CopilotReviewEvent:
    """输出审查结果（自检节点）：verdict ∈ ok/repaired/corrected/mismatch/unverified。"""

    verdict: str
    issues: list[dict[str, str]]


@dataclass(frozen=True)
class CopilotApprovalEvent:
    """写工具需人工确认：run_id 供 resume 重放；tool/args/summary/level 构成证据包。

    ``approval_id`` 是这张审批单的持久化主键——前端据此「逐单」回传裁决（一张单一个
    decision，不再一刀切）；``summary`` 是人话操作摘要（动的是什么）、``level`` 是风险
    等级（low/medium/high）、``args`` 是原始参数（供展开审计）。前端据此渲染「决策依据」
    而非甩一个裸 JSON——高信噪比证据包。
    """

    approval_id: uuid.UUID
    run_id: str
    tool: str
    args: dict[str, Any]
    summary: str = ""
    level: str = "high"


@dataclass(frozen=True)
class CopilotDoneEvent:
    assistant_message_id: uuid.UUID


CopilotStreamEvent = (
    CopilotMetaEvent
    | CopilotStepEvent
    | CopilotDeltaEvent
    | CopilotReviewEvent
    | CopilotApprovalEvent
    | CopilotDoneEvent
)
