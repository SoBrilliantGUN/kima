"""分块基础：目标长度常量、token 估算、Chunk/ParentChunk 数据类、父级切分。

父级（section）按标题层级把文档切成「节」；无标题文档回退为固定窗口；
过小节合并、超大节拆分，目标每 parent ~1000–2000 token。
子级切分（content-aware）见 `block.py` + `registry.py` + `splitters/`。
"""

import re
from dataclasses import dataclass, field

# 目标长度（token 估算值，非精确 tokenizer）
CHILD_TARGET_MAX_TOKENS = 500  # child 目标 ~300–500 的上界
CHILD_OVERLAP_TOKENS = 50  # 超长硬切的重叠量
PARENT_TARGET_MIN_TOKENS = 1000
PARENT_TARGET_MAX_TOKENS = 2000
PARENT_FALLBACK_WINDOW_TOKENS = 1500

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符约 1 token/字，其余按 4 字符/token。

    仅用于分块长度判断与 `token_count` 落库，不需要精确 tokenizer。
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass
class Chunk:
    """子级检索单元（child）。"""

    content: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ParentChunk:
    """父级上下文单元（parent）：整节内容，不向量化。"""

    content: str
    heading_path: str = ""
    children: list[Chunk] = field(default_factory=list)


def heading_level(line: str) -> int | None:
    """返回标题层级（1–6），非标题返回 None。"""
    match = _HEADING_RE.match(line)
    return len(match.group(1)) if match else None


def heading_text(line: str) -> str:
    """返回去掉 `#` 前缀的标题文本。"""
    match = _HEADING_RE.match(line)
    return match.group(2).strip() if match else line.strip()


def split_by_lines(text: str, max_tokens: int) -> list[str]:
    """按行累加切分，保证每段不超过 `max_tokens`。"""
    lines = text.strip().split("\n")
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        candidate = current + [line]
        if current and estimate_tokens("\n".join(candidate)) > max_tokens:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current = candidate
    if current:
        chunks.append("\n".join(current))
    return chunks


def segment_parents(markdown: str) -> list[ParentChunk]:
    """父级切分：按标题层级切成节；无标题回退固定窗口。"""
    lines = markdown.split("\n")
    heading_indexes = [i for i, line in enumerate(lines) if heading_level(line) is not None]
    if not heading_indexes:
        return _window_parents(markdown)

    root_level = min(heading_level(lines[i]) or 6 for i in heading_indexes)

    sections: list[tuple[str, list[str]]] = []
    current_path = ""
    current_lines: list[str] = []
    for line in lines:
        level = heading_level(line)
        if level == root_level:
            if current_lines or current_path:
                sections.append((current_path, current_lines))
            current_path = heading_text(line)
            current_lines = [line]
        else:
            current_lines.append(line)
    sections.append((current_path, current_lines))

    parents = [
        ParentChunk(content="\n".join(block).strip(), heading_path=path)
        for path, block in sections
        if block
    ]
    return _split_large(_merge_small(parents))


def _window_parents(markdown: str) -> list[ParentChunk]:
    chunks = split_by_lines(markdown, PARENT_FALLBACK_WINDOW_TOKENS)
    return [ParentChunk(content=chunk) for chunk in chunks if chunk.strip()]


def _merge_small(parents: list[ParentChunk]) -> list[ParentChunk]:
    """把过小的节并入相邻节（向前），目标 >= PARENT_TARGET_MIN。"""
    if len(parents) <= 1:
        return parents
    merged: list[ParentChunk] = []
    for parent in parents:
        if merged and estimate_tokens(merged[-1].content) < PARENT_TARGET_MIN_TOKENS:
            merged[-1] = _merge_parent(merged[-1], parent)
        else:
            merged.append(parent)
    if len(merged) > 1 and estimate_tokens(merged[-1].content) < PARENT_TARGET_MIN_TOKENS:
        merged[-2] = _merge_parent(merged[-2], merged[-1])
        merged.pop()
    return merged


def _merge_parent(a: ParentChunk, b: ParentChunk) -> ParentChunk:
    return ParentChunk(
        content=f"{a.content}\n\n{b.content}".strip(),
        heading_path=a.heading_path,
        children=a.children + b.children,
    )


def _split_large(parents: list[ParentChunk]) -> list[ParentChunk]:
    """把超大节按窗口拆分，保证每 parent <= PARENT_TARGET_MAX。"""
    result: list[ParentChunk] = []
    for parent in parents:
        if estimate_tokens(parent.content) <= PARENT_TARGET_MAX_TOKENS:
            result.append(parent)
            continue
        chunks = split_by_lines(parent.content, PARENT_FALLBACK_WINDOW_TOKENS)
        result.extend(
            ParentChunk(content=chunk, heading_path=parent.heading_path)
            for chunk in chunks
            if chunk.strip()
        )
    return result
