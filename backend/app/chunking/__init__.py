"""内容感知父子分块包。

对外入口 `chunk_document(markdown) -> list[ParentChunk]`：
父级按标题切节，子级按块类型匹配 splitter 产出检索单元。
"""

from app.chunking.base import (
    Chunk,
    ParentChunk,
    estimate_tokens,
    segment_parents,
)
from app.chunking.block import Block, BlockType, split_blocks
from app.chunking.registry import get_splitter, split_children

__all__ = [
    "Chunk",
    "ParentChunk",
    "Block",
    "BlockType",
    "estimate_tokens",
    "segment_parents",
    "split_blocks",
    "get_splitter",
    "split_children",
    "chunk_document",
]


def chunk_document(markdown: str) -> list[ParentChunk]:
    """两级切割：父级切节 → 每节内容感知切子块。"""
    parents = segment_parents(markdown)
    for parent in parents:
        blocks = split_blocks(parent.content)
        parent.children = split_children(blocks, parent.heading_path)
    return parents
