"""敏感信息脱敏：模型输出 / 审计日志里的密钥、邮箱、手机号等打码替换。

注入闸（injection.py）管「不可信文本放行」，本模块管「可信文本里的敏感值不外泄」：
模型输出若混进 API key / 邮箱 / 手机号，不打码就直接返回给用户或写进事件日志。
脱敏是软处置（改写），不做 fail-closed veto——与注入闸「命中即拒」不同。
"""

import re
from typing import Any, cast

_REDACTED = "[已脱敏]"

# 敏感值正则：密钥 / 邮箱 / 手机号 / 证件号等，命中即打码。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # —— 中国场景 PII（身份证 / 银行卡 / 护照）。用 (?<!\d)(?!\d) 数字边界替代 \b ——
        # Python 的 \w 含 Unicode 字母，CJK 汉字与数字相邻时 \b 不成立，导致中文文本里
        # 「身份证号110101…」这类紧贴汉字的数字漏检；数字前后断言既能在中文语境命中，
        # 又避免从更长的数字串中截断。须排在手机号之前：18 位身份证若以 13-19 开头，
        # 会被无边界手机号正则 1[3-9]\d{9} 抢先截走前 11 位。
        r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",       # 身份证 18 位
        r"(?<!\d)62\d{14,17}(?!\d)",                      # 银行卡（银联 62 开头，16-19 位）
        r"(?<![A-Za-z0-9])[EeGg]\d{8}(?!\d)",             # 护照（E/G + 8 位数字）
        r"1[3-9]\d{9}",                                   # 中国大陆手机号
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",  # 邮箱
        r"\b\d{3}-\d{2}-\d{4}\b",                         # 美国 SSN
        r"sk-[a-zA-Z0-9]{20,}",                           # OpenAI 密钥
        r"sk-ant-[a-zA-Z0-9\-_]{20,}",                    # Anthropic 密钥
        r"ghp_[a-zA-Z0-9]{36}",                           # GitHub PAT
        r"AKIA[0-9A-Z]{16}",                              # AWS Access Key
    )
)

# 敏感键：payload 里这些 key 的整值直接打码（不逐字匹配）。
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_token",
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "access_key",
        "secret_key",
        "private_key",
    }
)


def redact_sensitive(text: str) -> str:
    """把文本里命中的敏感值替换为占位符，返回改写后的文本。"""
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏事件 payload：敏感 key 整值打码，敏感 pattern 命中打码。"""

    def _scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: _REDACTED if k.lower() in _SENSITIVE_KEYS else _scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        if isinstance(value, str):
            return redact_sensitive(value)
        return value

    return cast(dict[str, Any], _scrub(payload))
