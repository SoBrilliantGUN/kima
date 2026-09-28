"""上下文窗口 L0 / L2 / L4 的文本拼装：宪法加载 + L0 system prompt + L2 记忆块。

六层上下文窗口（见 ``docs/context-window-layering.md``）：

- **L0** = system prompt（宪法 + soul/user + 操作引导 + skills 列表），前缀稳定保 Prompt Cache。
- **L2** = ``[MEMORY]`` 召回记忆注入块（按需注入、随本轮 query 变化、可压缩）。
- **L4** = ``[REMINDER]`` 宪法尾部重放（首尾三明治的「尾」，对抗 Lost in the Middle）。

宪法文件 ``data/constitution.md``（身份 + 行为铁律 + 红线）**同时拼进 L0 头部与 L4 尾部**；
操作引导（``_MEMORY_GUIDANCE`` / ``render_tool_hint``）只进 L0 常规、不进宪法、不进 reminder。
召回记忆是 L2，不该焊进永不压缩的 L0——否则记忆一多就挤压系统指令，且每轮 recall 变化会
打爆 Prompt Cache 前缀命中。
"""

from app.agent.toolmeta import SideEffectLevel, ToolMeta, ToolRegistry
from app.chunking.base import estimate_tokens
from app.core.config import BACKEND_DIR
from app.models.copilot import CopilotMemory
from app.services.copilot import RecalledMemories

CONSTITUTION_PATH = BACKEND_DIR / "data" / "constitution.md"

# 约束子预算上限（占记忆块总 token 预算的比例）——红线必须在场，但不能独占全部预算、
# 把偏好/事实/情节全挤掉（宁可多占 Token ≠ 独占全部 Token）。
_CONSTRAINT_BUDGET_RATIO = 0.4

# 操作引导（只进 L0 常规，不进宪法、不进 reminder）。
_MEMORY_GUIDANCE = (
    "记忆分为四型：\n"
    "- 约束（constraint）：用户定下的硬规则/红线/禁令（如「禁止联网搜索敏感话题」），"
    "无条件必须遵守、最高优先级。\n"
    "- 偏好（preference）：用户反复表达的偏好/软规则/风格默认（如「回答要简洁」）。\n"
    "- 事实（fact）：关于用户的稳定事实（身份、职位、技术栈等），可用 entity_id 锚定。\n"
    "- 情节（episodic）：带时间锚点的具体事件（某次做了什么、发生了什么）。\n"
    "当对话中出现值得长期记住的内容时，主动调用 write_memory 记录到合适的那一型；"
    "不确定就用 episodic。不要每句话都写，只在确有价值时写。"
)


def render_tool_hint(registry: ToolRegistry) -> str:
    """从工具注册表派生 L0 工具提示：按副作用分组（只读 / 写 / 高危写）。

    名字与一行用途取自 ``ToolMeta.hint``（注册处声明、必填），不再手写工具名单——加/删
    工具自动反映，杜绝 ``_TOOL_HINT`` 那种「列 7 个、实际 17 个」的漂移。用途全文仍由
    ``bind_tools`` 的工具 description 承载，这里只给一份紧凑的「能力地图 + 副作用分组」。
    """

    def line(m: ToolMeta) -> str:
        return f"{m.name}（{m.hint}）"

    read = [m for m in registry.values() if m.is_readonly]
    write = [m for m in registry.values() if m.side_effect_level is SideEffectLevel.MEDIUM]
    danger = [m for m in registry.values() if m.side_effect_level is SideEffectLevel.HIGH]
    parts = ["工具使用："]
    if read:
        parts.append("只读：" + "、".join(line(m) for m in read) + "；")
    if write:
        parts.append("写（有副作用，调用前确认）：" + "、".join(line(m) for m in write) + "；")
    if danger:
        parts.append("高危写（会触发审批）：" + "、".join(line(m) for m in danger) + "。")
    return "".join(parts)


def load_constitution() -> str:
    """读 ``data/constitution.md`` 全文；缺失或为空则抛错（宪法必须存在）。"""
    if not CONSTITUTION_PATH.exists():
        raise FileNotFoundError(f"宪法文件缺失：{CONSTITUTION_PATH}")
    text = CONSTITUTION_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"宪法文件为空：{CONSTITUTION_PATH}")
    return text


def format_reminder() -> str:
    """L4 尾部重放：宪法全文（user 角色 + ``[REMINDER]`` 带内标记，每轮钉在输入末尾）。"""
    return f"[REMINDER]\n{load_constitution()}"


def _format_memories_budgeted(
    label: str, memories: list[CopilotMemory], budget: int
) -> tuple[str, int]:
    """按 token 预算贪心填充一组记忆：逐条估算 token，塞不下的跳过、继续试后边的。

    返回 (块文本, 已用 token)；只计记忆正文 token（标签/换行是固定开销），放不进任何一条则返回空串。
    """
    if not memories:
        return "", 0
    lines = [f"【{label}】"]
    used = 0
    for memory in memories:
        cost = estimate_tokens(memory.content)
        if used + cost > budget:
            continue
        lines.append(f"- {memory.content}")
        used += cost
    if len(lines) == 1:
        return "", used
    return "\n".join(lines), used


def assemble_system_prompt(
    *,
    soul: str,
    user: str,
    skills: list[str] | None = None,
    registry: ToolRegistry | None = None,
) -> str:
    """L0 system prompt：宪法 + soul/user + skills 列表 + 操作引导 + 工具提示。

    宪法在头部（身份 + 铁律），soul/user 次之（人设 + 档案），skills 列表（自定义 skill 的
    name/description，供模型决定是否 ``get_skill`` 加载全文）随后，操作引导在尾——同一份宪法
    也在 L4 尾部重放（见 ``format_reminder``），首尾三明治。

    ``registry`` 非 None 时追加 ``render_tool_hint`` 派生的工具提示（副作用分组）；None
    则不追加（无工具注册表的纯拼装/测试路径）。
    """
    sections: list[str] = [load_constitution()]

    if soul.strip():
        sections.append(f"【人设 / 说话风格】\n{soul.strip()}")
    if user.strip():
        sections.append(f"【用户档案】\n{user.strip()}")

    if skills:
        sections.append("【可用 Skills】\n" + "\n".join(f"- {s}" for s in skills))

    sections.append(_MEMORY_GUIDANCE)
    if registry:
        sections.append(render_tool_hint(registry))
    return "\n\n".join(sections)


def format_memory_block(recalled: RecalledMemories, max_tokens: int) -> str:
    """把四型召回记忆格式化成 L2 注入块（``[MEMORY]`` 前缀，空则返回空串）。

    顺序 = 约束 → 偏好 → 事实 → 情节（优先级从高到低，对齐「先占先得」的串行合并）。
    按 token 预算贪心填充：逐条估算，塞不下的跳过、继续试后边的；约束子预算 ≤ 40%——
    红线优先在场、但不独占全部预算，把偏好/事实/情节全挤掉。
    """
    groups: list[tuple[str, list[CopilotMemory]]] = [
        ("约束（硬规则/红线，必须遵守）", recalled.constraint),
        ("偏好（软规则/风格默认，长期有效）", recalled.preference),
        ("事实（稳定状态）", recalled.fact),
        ("情节（具体事件）", recalled.episodic),
    ]
    constraint_budget = int(max_tokens * _CONSTRAINT_BUDGET_RATIO)
    blocks: list[str] = []
    remaining = max_tokens
    for label, memories in groups:
        budget = min(constraint_budget, remaining) if label.startswith("约束") else remaining
        block, used = _format_memories_budgeted(label, memories, budget)
        if block:
            blocks.append(block)
            remaining -= used
    if not blocks:
        return ""
    return "[MEMORY]\n" + "\n\n".join(blocks)


def format_subagent_constraints(recalled: RecalledMemories) -> str:
    """子 Agent 精简约束块（父显式下传）：宪法铁律（红线）+ constraint 型硬约束。

    不传 preference/fact/episodic（检索子任务只需底线）；也不传主 Agent 的 soul/user 人设
    与工具清单——子 Agent 用自己的「检索子 Agent」角色 + 这份精简底线，对齐 Claude 子
    Agent 的强隔离（唯一通道是派发时显式拼入）。
    """
    parts = [f"[约束]\n{load_constitution()}"]
    if recalled.constraint:
        lines = ["- " + m.content for m in recalled.constraint]
        parts.append("硬约束（必须遵守）：\n" + "\n".join(lines))
    return "\n\n".join(parts)
