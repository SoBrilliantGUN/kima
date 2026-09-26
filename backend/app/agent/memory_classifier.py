"""记忆分类器：写入时用 LLM（temperature=0）确定性分类，替代「信任 Agent 自报 kind」。

对齐分路召回架构防线一：约束类记忆不能靠 Agent 随手挑 kind——一条「禁止 ORM」被当成
事实写进去，就会走向量 top-k、被相似度阈值漏掉。故在 `write_memory` 落库前加一道
确定性分类，把四型（约束/事实/偏好/情节）钉回正确桶。分类器**恒在场**（不可关闭）；
失败返回 `None` 回退到「信任 Agent 自报」。

分类 → 桶的映射（约束是硬召回、其余走混合召回）：
- constraint → `MemoryKind.CONSTRAINT`（硬规则/红线，无条件在场）
- fact       → `MemoryKind.FACT`（稳定事实，entity 覆盖）
- preference → `MemoryKind.PREFERENCE`（偏好/软规则）
- episodic   → `MemoryKind.EPISODIC`（带时间锚点的事件）
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.agent.gateway import LLMGateway
from app.integrations.llm import (
    ChatMessage,
    StructuredParseError,
    format_instructions,
    parse_json,
)
from app.models.copilot import MemoryKind

logger = logging.getLogger(__name__)

# memory_type 字符串 → MemoryKind 的权威映射（分类器输出只有这四个取值）
_TYPE_TO_KIND: dict[str, MemoryKind] = {
    "constraint": MemoryKind.CONSTRAINT,
    "fact": MemoryKind.FACT,
    "preference": MemoryKind.PREFERENCE,
    "episodic": MemoryKind.EPISODIC,
}


@dataclass(frozen=True)
class MemoryClassification:
    """分类结果：权威 kind + 可选 entity 锚点 + 约束触发条件。"""

    kind: MemoryKind
    entity_id: str | None = None
    trigger_conditions: dict[str, Any] | None = None


class MemoryClassifier(Protocol):
    """写入时分类：返回权威分类；失败返回 None（回退到「信任 Agent 自报」）。"""

    async def classify(self, raw: str) -> MemoryClassification | None: ...


class _MemoryClassOutput(BaseModel):
    """记忆分类的结构化契约：memory_type 四选一，其余字段按类型可选。

    字段语义写进 ``description``，``model_json_schema()`` 直接产出带语义的完整 schema，
    供提示词格式说明引用（不再手写 JSON 示例）。
    """

    memory_type: Literal["constraint", "fact", "preference", "episodic"] = Field(
        description="记忆类型：constraint=硬规则/红线（禁止/严禁/必须/永远不要）；"
        "fact=当前状态事实（是/现在是/用）；preference=偏好/软规则（尽量/习惯/回答要）；"
        "episodic=带时间锚点的事件（昨天/上周/上次）"
    )
    entity_id: str | None = Field(
        default=None, description="仅 fact 类型填稳定键（如 user:role），否则 null"
    )
    trigger_condition: dict[str, Any] | None = Field(
        default=None,
        description='仅 constraint 类型填触发条件对象（如 {"type":"domain","value":"database"}），'
        "否则 null",
    )


class LLMMemoryClassifier:
    """真实实现：temperature=0 轻量分类，JSON 输出，失败 fail-closed 回退 None（决策 D5）。

    回退方向安全：None 表示「不动 Agent 自报的 kind」，与现状一致（分类器是额外的确定性
    兜底，不是唯一防线）；绝不把「无法分类」静默降级成某个可能漏掉红线的桶。
    """

    _SYSTEM_PROMPT = (
        "你是 AI Agent 系统的记忆分类助手。把传入的记忆文本分类为四种类型之一。\n"
        "类型：\n"
        "1. constraint —— 硬规则、禁令、安全红线（禁止/严禁/不得/必须/永远不要）。"
        "无论任务是什么，都必须始终出现在上下文中。示例：「禁止使用 ORM」。\n"
        "2. fact —— 关于用户或世界的当前状态事实（是/现在是/用/当前版本）。"
        "同一实体变化时覆盖上一条事实。示例：「用户是后端工程师」。\n"
        "3. preference —— 软性引导、习惯、风格默认（偏好/习惯/尽量/回答要）。"
        "不是硬约束。示例：「回答尽量简洁」。\n"
        "4. episodic —— 带时间锚点的历史事件（昨天/上周/上次/某次）。"
        "示例：「昨天讨论了架构」。\n"
    ) + format_instructions(_MemoryClassOutput)

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def classify(self, raw: str) -> MemoryClassification | None:
        messages = [ChatMessage("system", self._SYSTEM_PROMPT), ChatMessage("user", raw)]
        try:
            result = await self._gateway.complete(
                "classifier", messages, temperature=0, max_tokens=256
            )
        except Exception as exc:
            # 网关失败（预算硬停/熔断/网络）→ fail-closed 回退 None（信任 Agent 自报 kind）
            logger.warning("记忆分类 LLM 调用失败：%s", exc)
            return None
        try:
            return self._parse_strict(result.content)
        except StructuredParseError as exc:
            logger.warning("记忆分类解析失败：%s", exc.message)
            return None

    @staticmethod
    def _parse_strict(raw: str) -> MemoryClassification:
        """严格解析：非对象 / memory_type 越界 / entity_id 类型错 / trigger_condition 类型错
        任一失败抛 StructuredParseError；不回退「默认桶」。"""
        out = parse_json(raw, _MemoryClassOutput)
        return MemoryClassification(
            kind=_TYPE_TO_KIND[out.memory_type],
            entity_id=out.entity_id,
            trigger_conditions=out.trigger_condition,
        )
