"""记忆冲突判定（LLM）：`ConflictJudge` 协议 + `LLMConflictJudge` 实现 + 判定契约。

从 `services/copilot.py` 抽出——写入路径的冲突裁决是独立关切（类比 `guardrail.review` 的
`LLMOutputReviewer`），自包含、可注入 Fake 做确定性测试。判定回退方向安全（只会「漏去重」
不会「错去重」）。
"""

import logging
from enum import StrEnum
from typing import Any, Protocol

from pydantic import RootModel, model_validator

from app.agent.gateway import LLMGateway
from app.integrations.llm import (
    ChatMessage,
    StructuredParseError,
    format_instructions,
    parse_json,
)

logger = logging.getLogger(__name__)


class ConflictVerdict(StrEnum):
    """LLM 冲突判定结果：duplicate=新记忆冗余（去重不写）、contradiction=新赢旧退场、none 都留。"""

    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    NONE = "none"


class ConflictJudge(Protocol):
    """把新内容与候选逐条比对，返回与候选对齐的判定结果（可注入 Fake 做确定性测试）。"""

    async def judge(self, new_content: str, candidates: list[str]) -> list[ConflictVerdict]: ...


class _ConflictOutput(RootModel[list[ConflictVerdict]]):
    """冲突判定的数组契约：元素只能是 duplicate/contradiction/none。

    RootModel 让 ``model_json_schema()`` 产出「裸数组」schema（保持既有输出契约不变）；
    长度检查是随候选数变化的动态业务规则，留在 ``_parse_strict`` 里做。
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        # 容忍 " DUPLICATE " / "None" 这类大小写与首尾空格偏差
        if isinstance(value, list):
            return [v.strip().lower() if isinstance(v, str) else v for v in value]
        return value


class LLMConflictJudge:
    """真实实现：一次 LLM 调用批量判定，返回 JSON 数组；解析失败回退 none（都留）。

    回退方向安全（只会「漏去重」不会「错去重」），但旧实现把「数组长度不符 / 非数组 /
    枚举越界」都静默补齐——解析失败导致某次去重永久失效且无任何痕迹。现在：严格校验
    类型/长度/枚举，语法坏 JSON 用 ``loads_json_repair`` 确定性修复（决策 D5），
    语义失败 fail-closed 回退全 none，不再「喂回 LLM 自纠错」。
    """

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def judge(self, new_content: str, candidates: list[str]) -> list[ConflictVerdict]:
        if not candidates:
            return []
        system = (
            "你是记忆去重助手。判断「新记忆」与每条「候选记忆」的关系，"
            "逐条输出 verdict，只能是 duplicate（重复、语义等价、没带来新东西——"
            "新记忆不落盘、保留旧的一条）/ contradiction（矛盾，新的赢，旧的一条退场）/ "
            "none（无关或同主题不同事实，都保留）。"
            f"输出数组长度必须与候选数一致（{len(candidates)} 条）。\n"
        ) + format_instructions(_ConflictOutput)
        user_lines = [f"新记忆：{new_content}"]
        for i, candidate in enumerate(candidates):
            user_lines.append(f"候选{i}：{candidate}")
        messages = [ChatMessage("system", system), ChatMessage("user", "\n".join(user_lines))]
        expected = len(candidates)
        try:
            result = await self._gateway.complete("judge", messages, temperature=0)
        except Exception as exc:
            # 网关失败（预算硬停/熔断/网络）→ fail-closed 回退全 none（都保留）
            logger.warning("记忆冲突判定 LLM 调用失败：%s", exc)
            return [ConflictVerdict.NONE] * expected
        try:
            return self._parse_strict(result.content, expected)
        except StructuredParseError as exc:
            logger.warning("记忆冲突判定解析失败：%s", exc.message)
            return [ConflictVerdict.NONE] * expected

    @staticmethod
    def _parse_strict(raw: str, expected: int) -> list[ConflictVerdict]:
        """严格解析：非数组 / 长度不符 / 枚举越界任一失败抛 StructuredParseError。"""
        out = parse_json(raw, _ConflictOutput)
        verdicts = list(out.root)
        if len(verdicts) != expected:
            raise StructuredParseError(f"数组长度应为 {expected}，实际 {len(verdicts)}")
        return verdicts
