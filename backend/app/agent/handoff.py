"""交棒包：主 agent → 子 agent 的下行任务载体（白名单构造，防上下文膨胀）。

子 agent 交棒只给「该给的」——任务描述 / 硬约束 / 工具白名单 / 输入引用 / 上个输出（截断），
不给历史 / 推理 / 完整数据。字段对齐子 Agent 协作的通用语义：引用（``input_refs``）而非内联，
工具白名单（``available_tools``）显式声明子 Agent 能调什么。
"""

from dataclasses import dataclass, field

_DEFAULT_PRIOR_OUTPUT_MAX_CHARS = 2000


@dataclass
class HandoffPacket:
    """主 agent → 子 agent 的交棒包（白名单构造，只装任务形状字段）。"""

    task_description: str = ""
    constraints: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    input_refs: dict[str, str] = field(default_factory=dict)
    prior_output: str = ""
    prior_output_max_chars: int = _DEFAULT_PRIOR_OUTPUT_MAX_CHARS

    def to_task_prompt(self) -> str:
        """拼成子 agent 的任务提示：引用而非内联；约束/工具白名单显式声明。"""
        lines = [self.task_description.strip(), ""]
        if self.prior_output:
            trimmed = self.prior_output[: self.prior_output_max_chars]
            if len(self.prior_output) > self.prior_output_max_chars:
                trimmed += (
                    f"\n…(truncated, {len(self.prior_output) - self.prior_output_max_chars}"
                    " more chars)"
                )
            lines.append("Prior agent output:")
            lines.append(trimmed)
            lines.append("")
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"  - {c}" for c in self.constraints)
        if self.available_tools:
            lines.append("Available tools:")
            lines.append("  - " + "\n  - ".join(self.available_tools))
        if self.input_refs:
            lines.append("Input references (resolve via tools, do not inline):")
            lines.extend(f"  - {name}: {handle}" for name, handle in self.input_refs.items())
        return "\n".join(lines)
