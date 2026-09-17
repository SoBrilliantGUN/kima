"""代码 splitter：按函数/类边界（顶层缩进）切分；无清晰边界的语言降级按行。

注：不引入 tree-sitter（重依赖），用「顶层缩进的函数/类声明行」做轻量边界识别，
覆盖 Python / JS / TS / Go / Rust 等常见语言的 `def`/`class`/`function`/`fn` 等关键字；
识别不到边界时按行切分，仍超长的块由 registry 落回 recursive 硬切兜底。
"""

import re

from app.chunking.base import CHILD_TARGET_MAX_TOKENS, estimate_tokens, split_by_lines

# 顶层（缩进 <= 4 空格）的函数/类声明行
_BOUNDARY_RE = re.compile(
    r"^(?: {0,4})(?:(?:async\s+)?def\b|class\b|(?:async\s+)?function\b|fn\b|fun\b|func\b)"
)

_FENCE_RE = re.compile(r"^\s*`{3,}|^\s*~{3,}")


def split_code(content: str) -> list[str]:
    body = _strip_fence(content)
    if estimate_tokens(body) <= CHILD_TARGET_MAX_TOKENS:
        return [body] if body.strip() else []

    lines = body.split("\n")
    boundaries = [i for i, line in enumerate(lines) if _BOUNDARY_RE.match(line)]
    if len(boundaries) < 2:
        return split_by_lines(body, CHILD_TARGET_MAX_TOKENS)

    chunks: list[str] = []
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def _strip_fence(content: str) -> str:
    lines = content.strip().split("\n")
    if lines and _FENCE_RE.match(lines[0]):
        lines = lines[1:]
    if lines and _FENCE_RE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip()
