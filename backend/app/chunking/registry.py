"""按块类型匹配 splitter；无匹配 → recursive 兜底；仍超长 → recursive 硬切兜底。"""

from collections.abc import Callable

from app.chunking.base import CHILD_TARGET_MAX_TOKENS, Chunk, estimate_tokens
from app.chunking.block import Block, BlockType
from app.chunking.splitters.code_ast import split_code
from app.chunking.splitters.faq import split_faq
from app.chunking.splitters.legal import split_legal
from app.chunking.splitters.recursive import split_recursive
from app.chunking.splitters.table import split_table

Splitter = Callable[[str], list[str]]

_SPLITTERS: dict[BlockType, Splitter] = {
    BlockType.TABLE: split_table,
    BlockType.CODE: split_code,
    BlockType.LEGAL: split_legal,
    BlockType.FAQ: split_faq,
}


def get_splitter(block_type: BlockType) -> Splitter:
    return _SPLITTERS.get(block_type, split_recursive)


def split_children(blocks: list[Block], heading_path: str) -> list[Chunk]:
    """逐块切分产出 child，附着 `block_type` + `heading_path` 元数据。"""
    chunks: list[Chunk] = []
    current_heading = ""
    for block in blocks:
        if block.type == BlockType.HEADING:
            current_heading = block.content
            continue
        splitter = get_splitter(block.type)
        for piece in splitter(block.content):
            for sub in _ensure_size(piece):
                chunks.append(
                    Chunk(
                        content=sub,
                        metadata={
                            "block_type": block.type.value,
                            "heading_path": _join_heading(heading_path, current_heading),
                        },
                    )
                )
    return chunks


def _ensure_size(piece: str) -> list[str]:
    if not piece.strip():
        return []
    if estimate_tokens(piece) <= CHILD_TARGET_MAX_TOKENS:
        return [piece]
    return [p for p in split_recursive(piece) if p.strip()]


def _join_heading(parent: str, current: str) -> str:
    if parent and current:
        return f"{parent} > {current}"
    return current or parent
