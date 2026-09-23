"""输出审查（guardrail）：内容层——reviewer 协议 + LLM 实现 + 判定契约。

Agent 会在不调用工具的情况下口头声称「记好啦 / 写好了」，而工具回环不会复核最终回答
是否真有其事。本模块是「内容层」，图内节点层（`build_review_node` / `route_after_review`）
见 `review_node.py`。

内容层包含：
- 判定契约：`ReviewVerdict` / `ReviewIssue` / `ReviewResult` + `OutputReviewer` 协议。
- `LLMOutputReviewer`：LLM 出 JSON 判定，解析失败 fail-closed。
- `SideEffectVerifier` 协议：确定性副作用对账（与 LLM 判断互补）。

判定的目标错误（evidence）：
- ``no_tool_call``：回答声称完成了写操作（create_note / write_memory / 更新 soul·user），
  但轨迹里并未实际调用对应工具。
- ``tool_failed``：写工具被调用了，但返回了失败（如「创建失败」「读取失败」）。
- ``side_effect_missing``：`SideEffectVerifier` 回查 DB 发现写工具声称成功、副作用却
  未持久化（确定性对账，不依赖 LLM 判断，防「执行模型伪造证据骗校验模型」）。
- ``review_unavailable``：审查器本身失败（LLM 异常 / JSON 解析失败），fail-closed。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agent.gateway import LLMGateway
from app.agent.runtime.budget import BudgetExceeded
from app.integrations.llm import (
    ChatMessage,
    StructuredParseError,
    first_validation_error,
    loads_json_repair,
)

REVIEW_NODE = "review"
REVIEW_TOOL_NAME = "review"


class ReviewVerdict(StrEnum):
    """审查判定：ok 通过 / mismatch 不一致 / unverified 审查器无法判定（fail-closed）。"""

    OK = "ok"
    MISMATCH = "mismatch"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ReviewIssue:
    """一条不一致：回答声称做的事、应调用的工具、证据类型。"""

    claim: str
    tool: str
    evidence: str  # no_tool_call | tool_failed | side_effect_missing


@dataclass(frozen=True)
class ReviewResult:
    verdict: ReviewVerdict
    issues: list[ReviewIssue] = field(default_factory=list)


class OutputReviewer(Protocol):
    """把最终回答与工具轨迹对账（可注入 Fake 做确定性测试）。"""

    async def review(self, final_answer: str, trace: str) -> ReviewResult: ...


class _ReviewOutput(BaseModel):
    """审查判定的结构化契约：verdict 只允许 ok/mismatch，其余一律视为「审查不可用」。

    ``json.loads`` 只保证语法合法，
    本模型保证字段名与枚举值合法。verdict 缺字段、拼错、或取值越界（如 "maybe"）都会
    触发 ValidationError → fail-closed 判 UNVERIFIED，而不是像手写 ``.get()`` 那样
    静默落到 OK——那正是「格式完美的幻觉值直接污染下游」的入口。
    """

    verdict: Literal["ok", "mismatch"]
    issues: list[Any] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value: Any) -> Any:
        # 容忍 "OK" / " ok " 这类细微格式偏差
        return value.strip().lower() if isinstance(value, str) else value


class LLMOutputReviewer:
    """真实实现：一次 LLM 调用输出 JSON 判定；解析/结构失败 fail-closed（UNVERIFIED）。

    审查器自己失能时绝不「放行」——否则等价于「AI 觉得完成就完成」的反面形态：
    守门员开门放行。fail-closed 把「审查不可用」显式暴露，由 review 节点追加诚实更正。

    语法层坏 JSON 用 ``loads_json_repair`` 确定性修复（决策 D5），不再「喂回 LLM 自纠错」
    多烧一次调用；语义层失败直接 fail-closed。
    """

    _SYSTEM = (
        "你是输出审查器，核对「助手最终回答」与「本轮实际工具调用轨迹」是否一致。"
        "重点抓一类错误：助手在回答里声称完成了某个写操作（新建笔记 create_note、"
        "写长期记忆 write_memory、更新档案 update_profile），但轨迹里并未实际调用对应工具，"
        "或该工具返回了失败（如「创建失败」「读取失败」「写入失败」）。"
        "只输出 JSON，形如 "
        '{"verdict":"ok","issues":[{"claim":"声称做的事","tool":"对应工具名",'
        '"evidence":"no_tool_call"}]}。一致且无虚假完成时 verdict=ok、issues 为空数组。'
    )

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def review(self, final_answer: str, trace: str) -> ReviewResult:
        user = (
            f"助手最终回答：\n{final_answer}\n\n"
            f"本轮工具调用轨迹：\n{trace}\n\n只输出 JSON。"
        )
        messages = [ChatMessage("system", self._SYSTEM), ChatMessage("user", user)]
        try:
            result = await self._gateway.complete("review", messages, temperature=0)
        except BudgetExceeded:
            # 预算硬停：中止 run（决策 D2「该停就停」），不吞成 UNVERIFIED
            raise
        except Exception:
            # LLM 异常（熔断/超时/网络，网关重试后仍失败）fail-closed：审查器失能时绝不放行
            return ReviewResult(verdict=ReviewVerdict.UNVERIFIED, issues=[])
        try:
            return self._parse_strict(result.content)
        except StructuredParseError:
            return ReviewResult(verdict=ReviewVerdict.UNVERIFIED, issues=[])

    @staticmethod
    def _parse(raw: str) -> ReviewResult:
        """单次解析（测试用）：任何结构/语义失败都 fail-closed 判 UNVERIFIED。"""
        try:
            return LLMOutputReviewer._parse_strict(raw)
        except StructuredParseError:
            return ReviewResult(verdict=ReviewVerdict.UNVERIFIED, issues=[])

    @staticmethod
    def _parse_strict(raw: str) -> ReviewResult:
        """严格解析：语法/结构/语义任一失败抛 StructuredParseError（带精确错误）。"""
        data = loads_json_repair(raw)
        try:
            out = _ReviewOutput.model_validate(data)
        except ValidationError as exc:
            raise StructuredParseError(first_validation_error(exc)) from exc
        verdict = (
            ReviewVerdict.MISMATCH
            if out.verdict == ReviewVerdict.MISMATCH.value
            else ReviewVerdict.OK
        )
        # issues 属非核心字段：宽松提取（丢弃非对象项、强转字符串），不因单条细节失配而否定判定
        issues = [
            ReviewIssue(
                claim=str(item.get("claim", "")),
                tool=str(item.get("tool", "")),
                evidence=str(item.get("evidence", "no_tool_call")),
            )
            for item in out.issues
            if isinstance(item, dict)
        ]
        return ReviewResult(verdict=verdict, issues=issues)


class SideEffectVerifier(Protocol):
    """写工具副作用确定性对账：回查 DB 确认声称成功写入的内容真的存在。

    与 `OutputReviewer`（LLM 判断）互补——后者可被「伪造证据」蒙骗，本协议用确定性
    回查兜底：工具返回了 id，就回查该 id 是否真实持久化。返回 None 表示确认，否则
    返回失败原因字符串（供 review 节点强制判 mismatch）。
    """

    async def verify(self, tool_name: str, args: dict[str, Any], result: str) -> str | None: ...
