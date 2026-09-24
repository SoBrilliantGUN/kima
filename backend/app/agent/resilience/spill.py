"""工具结果 spill：大结果落盘 + 占位符 + 按需查，替代截断丢信息（决策 #26）。

读/检索工具返回超 `max_result_chars` 时不截断，而是把全文落到一个临时目录，返回
带 preview + 路径的占位符，Agent 可用 `read_tool_result` 工具按路径/grep 查询。
路径解析限制在 spill 目录内，防目录穿越。
"""

import hashlib
import tempfile
from pathlib import Path

_PREVIEW_CHARS = 2000


class SpillStore:
    """一次 run 的结果落盘目录（闭包捕获，per-request）。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="kima-spill-"))
        self._dir.mkdir(parents=True, exist_ok=True)

    def spill(self, content: str, tool_name: str) -> str:
        """落盘全文，返回带 preview + 路径的占位符。"""
        digest = hashlib.sha256(f"{tool_name}:{content}".encode()).hexdigest()[:16]
        path = self._dir / f"{tool_name}_{digest}.txt"
        path.write_text(content, encoding="utf-8")
        preview = content[:_PREVIEW_CHARS]
        if len(content) > _PREVIEW_CHARS:
            preview += "\n…（已截断预览）"
        return (
            f"[{tool_name}的结果已落盘，共 {len(content)} 字符]\n"
            f"预览（前 {_PREVIEW_CHARS} 字符）：\n{preview}\n"
            f"用 read_tool_result(path='{path.name}') 查询全文。"
        )

    def resolve(self, name: str) -> Path:
        """把路径名解析到 spill 目录内，拒绝越界/符号链接。"""
        raw = Path(name)
        if raw.is_absolute():
            raise ValueError(f"拒绝越界路径：{name!r}")
        target = (self._dir / raw.name).resolve()
        base = self._dir.resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"拒绝越界路径：{name!r}")
        return target

    def read(self, name: str) -> str | None:
        resolved = self.resolve(name)
        if not resolved.exists():
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
