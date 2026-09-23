"""意图路由：混合分类（规则第一刀 + LLM 兜底），决策 #22。

规则第一刀用关键词/注入检测判「投诉」与「注入」两类高风险意图（省成本 + 路由抗注入）；
未命中默认走 task（reactive 回环天然覆盖普通问答）。qa 细分与 LLM 兜底留 P2。
注入**红线**复用 `guardrail.injection.matches_injection_pattern`（硬正则）；祈使句浓度属
软信号，交给信任评分层（`guardrail.trust`）打分，不在路由层一刀切。
"""

from enum import StrEnum

from app.agent.guardrail.injection import matches_injection_pattern

# 投诉/负面情绪关键词（规则第一刀）
_COMPLAINT_HINTS = (
    "投诉", "气死", "差评", "太差了", "垃圾", "客服", "退钱", "退款", "要举报",
)
# 长程多步任务关键词（规则第一刀 → planner 模式）
_PLAN_HINTS = ("总结", "归纳", "调研", "报告", "对比", "分析", "整理", "汇总")
# 事实型问答关键词（规则第一刀 → RAG 直答，不进工具循环）
_QA_HINTS = ("是什么", "什么是", "为什么", "怎么", "如何", "定义", "解释", "介绍一下")


class Intent(StrEnum):
    TASK = "task"          # 办事：检索/读/写/联网（默认，reactive）
    QA = "qa"              # 问答：基于资料回答，不写（P2 细分）
    PLAN = "plan"          # 长程多步任务 → planner 模式
    COMPLAINT = "complaint"  # 投诉/负面情绪 → 确定性安抚流程
    INJECTION = "injection"  # 提示注入/越权 → 拒绝


def classify_by_rules(text: str) -> Intent | None:
    """规则第一刀：命中返回对应意图，否则 None（交给兜底）。"""
    if matches_injection_pattern(text):
        return Intent.INJECTION
    if any(h in text for h in _COMPLAINT_HINTS):
        return Intent.COMPLAINT
    if any(h in text for h in _PLAN_HINTS):
        return Intent.PLAN
    if any(h in text for h in _QA_HINTS):
        return Intent.QA
    return None


def classify_intent(text: str) -> Intent:
    """混合分类入口：规则第一刀；未命中默认 task。"""
    return classify_by_rules(text) or Intent.TASK
