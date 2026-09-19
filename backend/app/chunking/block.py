"""markdown → 语义块切分（markdown-it-py AST）+ 父级切分。

把 markdown 解析为异质块：标题 / 段落 / 代码围栏 / 表格 / 列表 / 引用，
每块附着 `heading_path`（所属标题全路径）。父级按「重复出现的最浅标题层级」切节，
供 `registry.py` 按块类型匹配 splitter 产出 child。
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.chunking.base import (
    PARENT_FALLBACK_WINDOW_TOKENS,
    PARENT_TARGET_MAX_TOKENS,
    PARENT_TARGET_MIN_TOKENS,
    ParentChunk,
    estimate_tokens,
    split_recursive,
)


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    BLOCKQUOTE = "blockquote"


@dataclass(frozen=True)
class Block:
    type: BlockType
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


@lru_cache
def _parser() -> MarkdownIt:
    """markdown-it-py 解析器（commonmark + GFM 表格）。"""
    return MarkdownIt("commonmark").enable("table")


# 顶层块类型（token.type → BlockType）
_BLOCK_OPEN: dict[str, BlockType] = {
    "paragraph_open": BlockType.PARAGRAPH,
    "table_open": BlockType.TABLE,
    "bullet_list_open": BlockType.LIST,
    "ordered_list_open": BlockType.LIST,
    "blockquote_open": BlockType.BLOCKQUOTE,
}


def split_blocks(markdown: str) -> list[Block]:
    """把 markdown 切成语义块（保持原顺序），每块 metadata 带完整 `heading_path`。

    只处理顶层（`level == 0`）块级 token，嵌套结构（列表项内段落等）由对应
    顶层容器（list/table/blockquote）整块承载，不再逐层拆解。
    """
    tokens = _parser().parse(markdown)
    lines = markdown.split("\n")
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []  # (标题层级, 标题文本)

    for i, tok in enumerate(tokens):
        if tok.level != 0:
            continue
        if tok.type == "heading_open":
            level = int(tok.tag[1])
            text = _heading_text(tokens, i)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            path = _join_path(stack)
            blocks.append(
                Block(BlockType.HEADING, text, {"level": str(level), "heading_path": path})
            )
        elif tok.type in _BLOCK_OPEN:
            blocks.append(
                Block(
                    _BLOCK_OPEN[tok.type],
                    _block_text(lines, tok),
                    {"heading_path": _join_path(stack)},
                )
            )
        elif tok.type == "fence":
            blocks.append(
                Block(
                    BlockType.CODE,
                    _block_text(lines, tok),
                    {"language": tok.info or "", "heading_path": _join_path(stack)},
                )
            )
        elif tok.type == "html_block":
            blocks.append(
                Block(
                    BlockType.PARAGRAPH,
                    _block_text(lines, tok),
                    {"heading_path": _join_path(stack)},
                )
            )
    return blocks


def _heading_text(tokens: list[Token], i: int) -> str:
    """取标题文本：`heading_open` 后紧跟的 `inline` token 的 content（不含 `#`）。"""
    if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
        return tokens[i + 1].content.strip()
    return ""


def _join_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(text for _, text in stack)


def _block_text(lines: list[str], tok: Token) -> str:
    """按 token 行号映射切回原始文本（保留表格/列表/代码围栏的原始格式）。"""
    if tok.map is None:
        return ""
    start, end = tok.map
    return "\n".join(lines[start:end]).strip()


def segment_parents(markdown: str) -> list[ParentChunk]:
    """父级切分：按标题层级切节；无标题文档回退递归切分（~1500 token/窗口）。"""
    return _group_blocks(split_blocks(markdown))


def _group_blocks(blocks: list[Block]) -> list[ParentChunk]:
    """把语义块按父级标题层级聚成节，做「过小合并 + 超大拆分」归一。"""
    headings = [b for b in blocks if b.type == BlockType.HEADING]
    if not headings:
        return _window_parents("\n\n".join(b.content for b in blocks))

    level = _parent_level(headings)
    parents: list[ParentChunk] = []
    current: list[Block] = []
    current_path = ""
    for block in blocks:
        if block.type == BlockType.HEADING and int(block.metadata["level"]) == level:
            if _has_content(current):
                parents.append(
                    ParentChunk(content=_parent_text(current), heading_path=current_path)
                )
                current = []
            current_path = block.metadata["heading_path"]
        current.append(block)
    if current:
        parents.append(ParentChunk(content=_parent_text(current), heading_path=current_path))
    return _split_large(_merge_small(parents))


def _parent_level(headings: list[Block]) -> int:
    """父级切分层级：重复出现的最浅标题层级；无重复则取最浅层级。

    例：`# A / # B` → 1；`# 标题 / ## A / ## B` → 2（标题作前言并入首节，
    避免整篇塌成一个大 parent）。
    """
    counts = Counter(int(h.metadata["level"]) for h in headings)
    for level in sorted(counts):
        if counts[level] >= 2:
            return level
    return min(counts)


def _has_content(blocks: list[Block]) -> bool:
    """当前分组是否含非标题内容（避免把纯前言标题单独切成空节）。"""
    return any(b.type != BlockType.HEADING for b in blocks)


def _parent_text(blocks: list[Block]) -> str:
    """把一组块拼成 parent 全文（标题补回 `#` 层级前缀，其余用原内容）。"""
    parts: list[str] = []
    for block in blocks:
        if block.type == BlockType.HEADING:
            level = int(block.metadata["level"])
            parts.append("#" * level + " " + block.content)
        else:
            parts.append(block.content)
    return "\n\n".join(parts).strip()


def _window_parents(markdown: str) -> list[ParentChunk]:
    chunks = split_recursive(markdown, PARENT_FALLBACK_WINDOW_TOKENS)
    return [ParentChunk(content=chunk) for chunk in chunks]


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
    # 合并后 heading_path 取更具体（更长）的路径，避免前言标题吞掉更深路径
    path = a.heading_path
    if len(b.heading_path) > len(path):
        path = b.heading_path
    return ParentChunk(
        content=f"{a.content}\n\n{b.content}".strip(),
        heading_path=path,
        children=a.children + b.children,
    )


def _split_large(parents: list[ParentChunk]) -> list[ParentChunk]:
    """把超大节递归切分，保证每 parent <= PARENT_TARGET_MAX。"""
    result: list[ParentChunk] = []
    for parent in parents:
        if estimate_tokens(parent.content) <= PARENT_TARGET_MAX_TOKENS:
            result.append(parent)
            continue
        chunks = split_recursive(parent.content, PARENT_FALLBACK_WINDOW_TOKENS)
        result.extend(
            ParentChunk(content=chunk, heading_path=parent.heading_path)
            for chunk in chunks
        )
    return result
