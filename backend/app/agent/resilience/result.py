"""工具结构化失败（反馈契约）：红绿灯 + 可操作 hint。

工具失败不是一句给人看的话，而是带 ``outcome``（红/黄灯）+ ``reason``（受控原因）+
``hint``（下一步引导）的强类型信号：
- 黄灯 ``TRANSIENT``：瞬时错误（超时/网络抖动），Loop 退避重试；
- 红灯 ``PERMANENT``：永久失败（不存在/非法入参），Loop 告知模型「换策略、别重试」。

传统报错给人看，Agent 工具的报错给模型看——可操作性比详细度更重要。这个信号是 Loop
做「重试 / 换策略」裁决的依据；对模型呈现的文本由 :meth:`ToolFailure.to_message` 生成。
"""

from enum import StrEnum


class ToolOutcome(StrEnum):
    """工具结局：绿灯 OK / 黄灯 TRANSIENT（可重试）/ 红灯 PERMANENT（别重试）。"""

    OK = "ok"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


class ToolFailure(Exception):
    """工具结构化失败：outcome 决定重试策略，reason/hint 供模型理解下一步。"""

    def __init__(
        self,
        *,
        outcome: ToolOutcome,
        reason: str,
        hint: str = "",
        code: str = "tool_error",
    ) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.hint = hint
        self.code = code

    @property
    def retryable(self) -> bool:
        """黄灯（瞬时）可重试；红灯（永久）不可。"""
        return self.outcome is ToolOutcome.TRANSIENT

    def to_message(self) -> str:
        """给模型看的红绿灯文本：永久失败显式标注「不要重试」并给出 hint。"""
        if self.outcome is ToolOutcome.TRANSIENT:
            head = "⚠️ 工具遇到临时错误（重试后仍失败）"
        else:
            head = "❌ 工具永久失败（请勿重复重试，换一种方式）"
        parts = [head, f"原因：{self.reason}"]
        if self.hint:
            parts.append(f"建议：{self.hint}")
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.to_message()
