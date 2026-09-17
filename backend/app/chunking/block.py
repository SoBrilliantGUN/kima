"""markdown → 语义块切分。

把 markdown 识别为异质块：标题 / 段落 / 代码围栏 / 表格 / 列表 / 法律条文 / FAQ 问答，
供 `registry.py` 按块类型匹配对应 splitter 产出 child。
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.chunking.base import heading_level


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    LEGAL = "legal"
    FAQ = "faq"


@dataclass(frozen=True)
class Block:
    type: BlockType
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_LEGAL_RE = re.compile(r"^第\s*[一二三四五六七八九十百千万零〇\d]+\s*[条款项]")
_FAQ_Q_RE = re.compile(r"^\s*(?:Q|问)\s*[:：]\s*\S", re.IGNORECASE)
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and "|" in line[1:]


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line)) and "-" in line


def split_blocks(markdown: str) -> list[Block]:
    """把 markdown 切成语义块（保持原顺序）。"""
    lines = markdown.split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # 标题
        level = heading_level(line)
        if level is not None:
            from app.chunking.base import heading_text

            blocks.append(Block(BlockType.HEADING, heading_text(line), {"level": str(level)}))
            i += 1
            continue

        # 代码围栏
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(2)
            language = stripped[len(fence.group(1)) + len(marker):].strip()
            buf = [line]
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                buf.append(lines[i])
                i += 1
            if i < n:
                buf.append(lines[i])
                i += 1
            blocks.append(Block(BlockType.CODE, "\n".join(buf), {"language": language}))
            continue

        # 表格（首行含 | 且下一行是分隔行）
        if _is_table_row(stripped) and i + 1 < n and _is_table_separator(lines[i + 1].strip()):
            buf = [line]
            i += 1
            while i < n and _is_table_row(lines[i].strip()):
                buf.append(lines[i])
                i += 1
            blocks.append(Block(BlockType.TABLE, "\n".join(buf)))
            continue

        # 列表
        if _LIST_RE.match(stripped):
            buf = [line]
            i += 1
            while i < n and (not lines[i].strip() or _LIST_RE.match(lines[i].strip())):
                buf.append(lines[i])
                i += 1
            blocks.append(Block(BlockType.LIST, "\n".join(buf)))
            continue

        # 法律条文
        if _LEGAL_RE.match(stripped):
            buf = [line]
            i += 1
            while i < n and lines[i].strip() and not _LEGAL_RE.match(lines[i].strip()):
                buf.append(lines[i])
                i += 1
            blocks.append(Block(BlockType.LEGAL, "\n".join(buf)))
            continue

        # FAQ 问答对
        if _FAQ_Q_RE.match(stripped):
            buf = [line]
            i += 1
            while i < n and lines[i].strip() and not _is_paragraph_break(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(Block(BlockType.FAQ, "\n".join(buf)))
            continue

        # 普通段落
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            buf.append(lines[i])
            i += 1
        blocks.append(Block(BlockType.PARAGRAPH, "\n".join(buf)))

    return blocks


def _is_block_start(line: str) -> bool:
    stripped = line.strip()
    return bool(
        heading_level(line) is not None
        or _FENCE_RE.match(line)
        or (_is_table_row(stripped) and "|" in stripped)
        or _LIST_RE.match(stripped)
        or _LEGAL_RE.match(stripped)
        or _FAQ_Q_RE.match(stripped)
    )


def _is_paragraph_break(line: str) -> bool:
    """FAQ 块内判断是否遇到下一段（新标题/新块起头）。"""
    return _is_block_start(line)
