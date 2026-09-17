"""内容感知 splitter 集合。"""

from app.chunking.splitters.code_ast import split_code
from app.chunking.splitters.faq import split_faq
from app.chunking.splitters.legal import split_legal
from app.chunking.splitters.recursive import split_recursive
from app.chunking.splitters.table import split_table

__all__ = ["split_recursive", "split_table", "split_code", "split_legal", "split_faq"]
