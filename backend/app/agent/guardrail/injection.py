"""注入四道闸的扫描机制：L1 入口 / L2 检索 / L3 工具参数 / L4 输出，决策 #23。

机制与策略分离：`scan_text` 是机制（命中即抛 PromptInjectionDetected），
`InjectionPolicy` 是应用注入的策略（编译正则集）。安全闸 fail-closed——命中即 veto，
绝不把不可信文本放行进模型或工具。

相较早期的子串黑名单，这里改用**编译正则 + 词边界 + 容忍空白**：
- 词边界 ``\\b`` 消掉「你是一个 / 泄露 / system prompt」这类子串误报
  （子串匹配会命中「你是一个什么助手」「如何防止信息泄露」）。
- ``\\s*`` 容忍「忽略 之前的 指令」这类空白改写，``IGNORECASE`` 容忍大小写混淆。
- 新增**格式逃逸**（``</system>``、``<|im_start|>``、``[INST]``）——伪造角色边界/
  指令头，是纯关键词匹配完全漏掉的一整类注入。

``ImperativeDensityGuard`` 是 L1 二级信号（祈使句浓度 + 指令名词 cue 双命中），
抓正则语料覆盖不到的改写注入；阈值命中即按注入拒答（接受少量误报，见决策）。

信任边界（谁可信、谁不可信）：系统提示词是可信方，任何扫描路径都不扫它；用户输入
（L1）、工具参数（L3）、工具返回（L2）、模型输出（L4）均是不可信方，逐一扫描。
"""

import re
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import DomainError


class PromptInjectionDetected(DomainError):
    """检测到提示注入，拒绝处理。附带 source / pattern 供审计结构化检索。"""

    code = "injection_detected"

    def __init__(self, message: str, *, source: str = "", pattern: str = "") -> None:
        super().__init__(message)
        self.source = source
        self.pattern = pattern


# 注入正则语料（英文 / 中文 / 格式逃逸）。词边界防子串误报，\\s* 容忍空白改写。
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        # —— 英文：要求忽略 / 遗忘先前的指令、规则 ——
        r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior|earlier)\s+"
        r"(?:instructions?|prompts?|rules?|context)\b",
        r"\bforget\s+(?:everything|all|your|the|previous|prior)\b",
        r"\b(?:disregard|override|bypass|supersede)\s+(?:all\s+)?"
        r"(?:previous|prior|above|your)?\s*"
        r"(?:instructions?|rules?|safety|guardrails?|prompts?|guidelines?)\b",
        # —— 英文：角色切换 / 解除限制 ——
        r"\byou\s+(?:are|act)\s+now\s+(?:a\s+)?"
        r"(?:different|new|another|unrestricted|uncensored|jailbroken|evil)\b",
        r"\byou\s+(?:are|act)\s+(?:now\s+)?(?:an?\s+)?"
        r"(?:unrestricted|uncensored|DAN|jailbroken)\b",
        # —— 英文：要求泄露提示词 / 指令 ——
        r"\b(?:reveal|show|output|print|expose|dump)\s+(?:me\s+)?(?:your\s+)?"
        r"(?:system\s+prompt|instructions?|rules?)\b",
        # —— 格式逃逸：伪造 system / 角色边界或指令头 ——
        r"<\s*/?\s*system\s*>",
        r"<\s*\|?\s*im_start\s*\|?\s*>",
        r"<\s*\|?\s*(?:system|user|assistant)\s*\|?\s*>",
        r"<\s*\|?\s*endofprompt\s*\|?\s*>",
        r"\[\s*INST\s*\]",
        r"###\s*instructions?",
        # —— 中文：要求忽略 / 遗忘先前的指令、规则 ——
        r"忽略\s*(?:之前|以上|先前|此前|上面的)?\s*(?:的)?\s*(?:所有|一切)?\s*"
        r"(?:指令|提示|规则|设定|约束)",
        r"忘记\s*(?:所有|一切|之前|你的|先前的)?\s*(?:的)?\s*"
        r"(?:指令|规则|提示|设定|限制|约束)",
        r"无视\s*(?:之前|以上|所有|系统|你的)?\s*(?:的)?\s*"
        r"(?:指令|规则|限制|提示词|约束)",
        # —— 中文：角色切换 / 解除限制 ——
        r"你现在是|从现在开始你是|你现在扮演|从现在起你是|你将扮演",
        r"你是一个?(?:不受限|无限制|DAN|越狱|没有限制|无所不能)",
        # —— 中文：要求泄露提示词 / 指令 ——
        r"你(?:的)?(?:系统)?提示词",
        r"(?:泄露|透露|输出|打印|展示|说出|告诉我)\s*(?:你的|你的系统)?\s*"
        r"(?:提示词|系统提示词|指令|规则)",
    )
)


@dataclass(frozen=True)
class InjectionPolicy:
    """注入检测策略：编译正则集（空 = 关闭检测，pass-through）。"""

    injection_patterns: tuple[re.Pattern[str], ...] = _INJECTION_PATTERNS


DEFAULT_INJECTION_POLICY = InjectionPolicy()


class ImperativeDensityGuard:
    """祈使句浓度检测：L1 二级信号，抓正则语料覆盖不到的改写注入。

    思路：逐句算「祈使动词密度」，
    配合「指令名词 cue」双命中才 flag；先排除技术术语（覆盖率 / 忽略大小写 /
    绕过缓存 / override method 等）降误报。中文无空格，按单字分词（每个 CJK 字符 = 1 词）。
    阈值命中只作注入嫌疑，交由调用方处置（L1 直接拒答，接受少量误报）。
    """

    _IMPERATIVE = re.compile(
        r"\b(?:ignore|forget|disregard|override|bypass|supersede|obey)\b"
        r"|忽略|忘记|无视|覆盖|绕过|服从|扮演|抛弃|抹除",
        re.IGNORECASE,
    )
    _CUE = re.compile(
        r"\b(?:instructions?|prompts?|rules?|system\s+prompt|guidelines?|guardrails?|"
        r"safety\s+(?:rules?|guidelines?)|persona|identity)\b"
        r"|指令|规则|提示词|设定|人格|身份|限制|系统提示",
        re.IGNORECASE,
    )
    _EXCLUSIONS = re.compile(
        "|".join(
            (
                # 英文：技术语境下的 ignore / override / bypass（非注入祈使）
                r"\bignore\s+(?:case|warnings?|errors?|files?|whitespace|comments?)\b",
                r"\b(?:method|function|class)\s+overriding?\b",
                r"\boverride\s+(?:the\s+)?(?:method|function|constructor|toString|equals|hashCode)\b",
                r"\bbypass\s+(?:cache|proxy|cdn)\b",
                # 中文：覆盖率 / 忽略大小写 / 覆盖默认 / 绕过缓存 等技术语境
                r"(?:代码|语句|分支|路径|测试|方法|函数|行|判定|条件)覆盖",
                r"覆盖率",
                r"覆盖(?:默认|配置|值|参数|文件|安装|部署|式|版本)",
                r"忽略(?:大小写|空白|告警|警告|错误|异常|文件|换行|注释|目录|符号)",
                r"绕过(?:缓存|代理|防火墙|DNS|鉴权|登录|限流)",
            )
        ),
        re.IGNORECASE,
    )
    _SENTENCE_SPLIT = re.compile(r"[.!?\n。！？；;]+")
    _TOKEN = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")

    IMPERATIVE_DENSITY_THRESHOLD = 0.10

    def is_injection(self, text: str) -> bool:
        """祈使句浓度超阈 且 出现指令名词 cue 时判定为注入嫌疑。"""
        return self._max_sentence_density(text) > self.IMPERATIVE_DENSITY_THRESHOLD and bool(
            self._CUE.search(text)
        )

    def _max_sentence_density(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        clean = self._EXCLUSIONS.sub("", text)
        max_density = 0.0
        for sent in self._SENTENCE_SPLIT.split(clean):
            tokens = self._TOKEN.findall(sent)
            if not tokens:
                continue
            matches = self._IMPERATIVE.findall(sent)
            density = len(matches) / len(tokens)
            if density > max_density:
                max_density = density
        return max_density


DENSITY_GUARD = ImperativeDensityGuard()


def matches_injection_pattern(
    text: str, policy: InjectionPolicy = DEFAULT_INJECTION_POLICY
) -> bool:
    """红线：命中注入正则（不含浓度）。硬命中即毙，是信任评分层里唯一的一票否决。"""
    return any(pattern.search(text) for pattern in policy.injection_patterns)


def scan_text(text: str, policy: InjectionPolicy, source: str) -> None:
    """扫描文本；命中任一注入正则即抛 PromptInjectionDetected。"""
    for pattern in policy.injection_patterns:
        if pattern.search(text):
            raise PromptInjectionDetected(
                f"检测到提示注入（{source}）：{pattern.pattern}",
                source=source,
                pattern=pattern.pattern,
            )


def scan_value(value: Any, policy: InjectionPolicy, source: str) -> None:
    """递归扫描值（str/dict/list），用于工具参数。"""
    if isinstance(value, str):
        scan_text(value, policy, source)
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_value(item, policy, f"{source}.{key}")
    elif isinstance(value, list):
        for item in value:
            scan_value(item, policy, source)


def scan_tool_calls(tool_calls: Any, policy: InjectionPolicy) -> None:
    """扫描一轮工具调用的参数；命中即抛 PromptInjectionDetected（L3）。"""
    for tc in tool_calls or []:
        name = tc.get("name", "")
        scan_value(tc.get("args"), policy, f"tool_arg:{name}")
