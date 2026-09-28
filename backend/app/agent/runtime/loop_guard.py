"""防循环：死循环（工具指纹）+ 幽灵循环（上下文 hash），决策 #25。

死循环 = 同一工具 + 相同参数（剔除 limit/offset 等易变键）在窗口内重复 N 次；
幽灵循环 = 上下文（最近 6 条消息）hash 连续 N 轮不变（模型空转不产出新东西）。
检测器闭包捕获、不进 LangGraph state，故 checkpoint 续跑不会丢循环记忆（同一 run 内）。
"""

import hashlib
import json
from collections import deque
from typing import Any

from langchain_core.messages import AnyMessage

from app.core.exceptions import DomainError

# 创建指纹时剔除的「易变」参数键（每次查询都可能变，不构成死循环）
_VOLATILE_KEYS = frozenset({"limit", "offset", "page", "top_k"})

DEFAULT_REPEAT_THRESHOLD = 5
DEFAULT_WINDOW_SIZE = 5
DEFAULT_STALL_THRESHOLD = 4


class InfiniteLoopDetected(DomainError):
    """检测到死循环 / 幽灵循环，终止本次 run。"""

    code = "loop_detected"


def fingerprint(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        args = {}
    stable = {k: v for k, v in args.items() if k not in _VOLATILE_KEYS}
    payload = json.dumps({"name": name, "args": stable}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _context_hash(messages: list[AnyMessage]) -> str:
    recent = messages[-6:] if len(messages) >= 6 else messages
    parts: list[str] = []
    for msg in recent:
        role = getattr(msg, "type", type(msg).__name__)
        content = str(getattr(msg, "content", ""))
        tool_calls = str(getattr(msg, "tool_calls", ""))
        parts.append(f"{role}:{content}:{tool_calls}")
    return hashlib.md5("\n".join(parts).encode()).hexdigest()


class LoopGuard:
    """一次 run 的循环检测器（闭包捕获）。"""

    def __init__(
        self,
        repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
        window_size: int = DEFAULT_WINDOW_SIZE,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
    ) -> None:
        self._repeat_threshold = repeat_threshold
        self._fingerprints: deque[str] = deque(maxlen=window_size)
        self._stall_threshold = stall_threshold
        self._hashes: deque[str] = deque(maxlen=stall_threshold)

    def check_tool_calls(self, tool_calls: Any) -> None:
        """每个工具调用前指纹；同指纹在窗口内达到阈值则判死循环。"""
        for tc in tool_calls or []:
            name = tc.get("name", "")
            fp = fingerprint(name, tc.get("args"))
            self._fingerprints.append(fp)
            if self._fingerprints.count(fp) >= self._repeat_threshold:
                raise InfiniteLoopDetected(
                    f"死循环：工具 {name} 相同参数在窗口内重复 {self._repeat_threshold} 次"
                )

    def check_context(self, messages: list[AnyMessage]) -> None:
        """进模型前查上下文 hash；连续 stall_threshold 轮不变则判幽灵循环。"""
        h = _context_hash(messages)
        self._hashes.append(h)
        if len(self._hashes) >= self._stall_threshold and len(set(self._hashes)) == 1:
            raise InfiniteLoopDetected(f"幽灵循环：上下文连续 {self._stall_threshold} 轮不变")
