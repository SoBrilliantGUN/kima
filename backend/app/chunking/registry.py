"""按块类型匹配 splitter；无匹配 → recursive 兜底；仍超长 → recursive 硬切兜底。

标题块作为独立 child 落地（可检索），不再仅存 metadata。
"""

from collections.abc import Callable

from app.chunking.base import CHILD_TARGET_MAX_TOKENS, Chunk, estimate_tokens, split_recursive
from app.chunking.block import Block, BlockType
from app.chunking.splitters.code_ast import split_code
from app.chunking.splitters.table import split_table

Splitter = Callable[[str], list[str]]

_SPLITTERS: dict[BlockType, Splitter] = {
    BlockType.TABLE: split_table,
    BlockType.CODE: split_code,
}


def get_splitter(block_type: BlockType) -> Splitter:
    return _SPLITTERS.get(block_type, split_recursive)


def split_children(blocks: list[Block]) -> list[Chunk]:
    """逐块切分产出 child，附着 `block_type` + `heading_path` 元数据；标题即 child。"""
    chunks: list[Chunk] = []
    for block in blocks:
        metadata = {**block.metadata, "block_type": block.type.value}
        if block.type == BlockType.HEADING:
            chunks.append(Chunk(content=block.content, metadata=metadata))
            continue
        splitter = get_splitter(block.type)
        for piece in splitter(block.content):
            for sub in _ensure_size(piece):
                chunks.append(Chunk(content=sub, metadata=metadata))
    return chunks


def _ensure_size(piece: str) -> list[str]:
    if not piece.strip():
        return []
    if estimate_tokens(piece) <= CHILD_TARGET_MAX_TOKENS:
        return [piece]
    return [p for p in split_recursive(piece) if p.strip()]
