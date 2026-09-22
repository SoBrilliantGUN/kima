"""零信任评分：三维度（内容/来源/行为）+ 红线 + 加权综合 + 分级处置。

核心思想：默认不信任任何进入上下文的数据，给每份数据打 0-100 可信度分，按分处置。

- **红线**（硬注入正则）命中 → 直接阻断，不进评分、不做加权——这是唯一的一票否决。
- **其余** → 内容/来源/行为三维度加权平均得综合分，映射五档处置：
  放行 / 观察（审计）/ 隔离（<data> 标签）/ 脱敏 / 阻断。
- 综合分随 run 流动、**只减不增**（各节点用 min 合并）。

权重与阈值是**先拍的假设值**（无红队/事故/正常流量标注集、无 ROC/PR 曲线），
留作常量便于拿到样本后校准。来源评级动态化属「持续演化」，暂不做（静态表）。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.agent.guardrail.injection import DENSITY_GUARD, matches_injection_pattern
from app.agent.guardrail.sensitive import redact_sensitive


class Disposition(StrEnum):
    """分级处置：按综合分从宽到严。"""

    PASS = "pass"              # 放行
    OBSERVE = "observe"        # 观察（完整审计，照常处理）
    QUARANTINE = "quarantine"  # 隔离（关进 <data> 标签，禁止执行）
    REDACT = "redact"          # 脱敏（过滤敏感信息后处理）
    BLOCK = "block"            # 阻断（拒绝 + 告警）


# —— 来源可信度：数据出处的信任基线（静态表；动态评级属持续演化，暂不做）——
_SOURCE_TRUST = {
    "system": 90,       # 系统提示词（内部系统生成）
    "user": 70,         # 认证用户输入
    "model": 60,        # 模型输出（可被诱导）
    "kb": 60,           # 企业知识库检索 / 读文档 / 读记忆
    "tool_result": 40,  # 第三方工具返回
    "web": 20,          # 公开网页搜索（完全不可控）
    "document": 60,     # 待入库文档（企业知识库，可能被投毒）
}
_DEFAULT_SOURCE_TRUST = 40  # 未知来源按第三方处理

# —— 内容软信号：祈使句密度命中扣分（红线已在外面直接毙）——
_DENSITY_DEDUCTION = 20

# —— 行为简化规则：短时高频写 + 写后立刻回读 ——
_WRITE_FREQ_THRESHOLD = 3
_WRITE_FREQ_DEDUCTION = 25
_WRITE_READ_BACK_DEDUCTION = 20

# —— 三维度权重（假设值，待标注样本校准）——
_W_CONTENT = 0.5
_W_SOURCE = 0.3
_W_BEHAVIOR = 0.2

# —— 处置阈值 ——
_PASS_MIN = 80
_OBSERVE_MIN = 60
_QUARANTINE_MIN = 40
_REDACT_MIN = 20


def is_red_line(text: str) -> bool:
    """红线：硬注入正则命中即毙。"""
    return matches_injection_pattern(text)


def source_trust(source: str) -> float:
    """来源可信度基线。"""
    return _SOURCE_TRUST.get(source, _DEFAULT_SOURCE_TRUST)


def content_trust(text: str) -> float:
    """内容可信度：100 − 20×密度命中（红线已在外毙掉）。"""
    return 100.0 - _DENSITY_DEDUCTION if DENSITY_GUARD.is_injection(text) else 100.0


def behavior_trust(write_count: int, write_then_read_back: bool) -> float:
    """行为可信度：简化规则扣分。"""
    score = 100.0
    if write_count >= _WRITE_FREQ_THRESHOLD:
        score -= _WRITE_FREQ_DEDUCTION
    if write_then_read_back:
        score -= _WRITE_READ_BACK_DEDUCTION
    return max(0.0, score)


def composite(content: float, source: float = 100.0, behavior: float = 100.0) -> float:
    """综合分：三维度加权平均（未评估的维度默认 100）。"""
    return _W_CONTENT * content + _W_SOURCE * source + _W_BEHAVIOR * behavior


def disposition(score: float) -> Disposition:
    """按综合分映射处置档位。"""
    if score >= _PASS_MIN:
        return Disposition.PASS
    if score >= _OBSERVE_MIN:
        return Disposition.OBSERVE
    if score >= _QUARANTINE_MIN:
        return Disposition.QUARANTINE
    if score >= _REDACT_MIN:
        return Disposition.REDACT
    return Disposition.BLOCK


def sanitize_content(content: str, source: str) -> tuple[str, float]:
    """对一份不可信内容做红线阻断 / 隔离 / 脱敏，返回 (处置后内容, 节点信任分)。

    供非 LangGraph 的执行路径（planner 工具结果）复用 reactive 里 `_evaluate_tool_results`
    的同一套处置逻辑：红线直接毙、其余按综合分五档处置、节点信任只减不增。
    """
    if is_red_line(content):
        return "（检测到注入内容，已阻断。）", 0.0
    score = composite(content_trust(content), source=source_trust(source))
    disp = disposition(score)
    if disp is Disposition.QUARANTINE:
        return f"<data>\n{content}\n</data>", score
    if disp is Disposition.REDACT:
        return f"<data>\n{redact_sensitive(content)}\n</data>", score
    if disp is Disposition.BLOCK:
        return "（检测到可疑内容，已阻断。）", score
    return content, score


@dataclass
class BehaviorTracker:
    """会话级行为状态（简化版）：统计写调用 + 检测「写后立刻回读」。

    由 service 在观察工具调用流时喂数据，L5 输出时取 `score()` 参与综合分。
    """

    write_count: int = 0
    _pending_write: bool = False
    write_then_read: bool = False
    write_tool_names: frozenset[str] = frozenset()

    def record(self, tool_name: str, args: dict[str, Any]) -> None:
        if tool_name in self.write_tool_names:
            self.write_count += 1
            self._pending_write = True
        elif self._pending_write:
            # 写后紧跟一个读（非写）→ 判「写后回读」
            self.write_then_read = True
            self._pending_write = False

    def score(self) -> float:
        return behavior_trust(self.write_count, self.write_then_read)
