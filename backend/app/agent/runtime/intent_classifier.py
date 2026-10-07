"""意图分类器：入口用 LLM（temperature=0）判 plan/qa/task，替代「信任关键词」。

对齐「复杂任务一开始就走 planner 图」的需求：plan（长程多步任务）需要按步数放大的
资源预算，若靠关键词「总结/调研」误判，复杂任务会被 reactive 小预算卡死（tokens/cost
轴超限）。故在规则第一刀（只判注入/投诉拒绝分支，见 ``router.py``）之后，加一道
确定性 LLM 分类，把 plan / qa / task 钉回正确意图。

分类器**恒在场**；失败返回 ``None`` 回退 TASK（reactive）——fail-open 方向安全：绝不把
「无法分类」静默升级成 plan（那会让简单任务白占大预算）。
"""

import logging
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.agent.gateway import LLMGateway
from app.agent.runtime.router import Intent
from app.integrations.llm import (
    ChatMessage,
    StructuredParseError,
    format_instructions,
    parse_json,
)

logger = logging.getLogger(__name__)

# 分类器输出的 intent 字符串 → Intent 枚举（只有这三个取值）
_INTENT_TO_ENUM: dict[str, Intent] = {
    "plan": Intent.PLAN,
    "qa": Intent.QA,
    "task": Intent.TASK,
}


class IntentClassifier(Protocol):
    """入口意图分类：返回权威意图；失败返回 None（回退 TASK/reactive）。"""

    async def classify(self, text: str) -> Intent | None: ...


class _IntentOutput(BaseModel):
    """意图分类的结构化契约：plan/qa/task 三选一。

    字段语义写进 ``description``，``format_instructions`` 直接产出带语义的 schema。
    """

    intent: Literal["plan", "qa", "task"] = Field(
        description="用户请求的意图：plan=需要先规划再执行的长程多步任务（遍历知识库、"
        "综合多来源、多步骤产出）；qa=基于已有资料回答问题、只读不写不执行；"
        "task=普通办事（检索/读/写/联网，边走边判断即可，无需显式规划）"
    )


class LLMIntentClassifier:
    """真实实现：temperature=0 轻量分类，JSON 输出，失败 fail-open 回退 None。

    ``gateway.complete`` 已 ``count_turn=False``（辅助调用不计 turn），只计 token/cost，
    符合「分类是入口路由、不占主循环轮次」的语义。
    """

    _SYSTEM_PROMPT = (
        "你是 AI Agent 系统的意图分类助手。把用户请求分为三类之一。\n"
        "1. plan —— 需要先规划再执行的长程多步任务（遍历/汇总/调研/多步产出）。\n"
        "2. qa —— 基于已有资料回答问题，只读不写不执行。\n"
        "3. task —— 普通办事（检索/读/写/联网），边走边判断即可，无需显式规划。\n"
    ) + format_instructions(_IntentOutput)

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def classify(self, text: str) -> Intent | None:
        messages = [ChatMessage("system", self._SYSTEM_PROMPT), ChatMessage("user", text)]
        try:
            result = await self._gateway.complete(
                "intent", messages, temperature=0, max_tokens=64
            )
        except Exception as exc:
            # 网关失败（预算硬停/熔断/网络）→ fail-open 回退 None（按 TASK 走 reactive）
            logger.warning("意图分类 LLM 调用失败：%s", exc)
            return None
        try:
            out = parse_json(result.content, _IntentOutput)
        except StructuredParseError as exc:
            logger.warning("意图分类解析失败：%s", exc.message)
            return None
        return _INTENT_TO_ENUM[out.intent]
