"""结构化递归 splitter（默认兜底）。

按「段落 → 行 → 句子（中/英文）→ 词」逐级拆成原子片段，再贪心合并回
~CHILD_TARGET_MAX token 的块；无法再拆的超长片段用 token 精确的硬切 + 重叠兜底。
处理对象：普通段落、列表等没有专用 splitter 的块。
"""

from app.chunking.base import (
    CHILD_OVERLAP_TOKENS,
    CHILD_TARGET_MAX_TOKENS,
    estimate_tokens,
)

# 分隔符优先级：段落 → 行 → 中文句 → 英文句 → 词
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", " "]

_CJK_START = "一"
_CJK_END = "鿿"


def _tokens_of_char(ch: str) -> float:
    """单字符 token 估算：CJK 约 1，其余约 1/4。"""
    return 1.0 if _CJK_START <= ch <= _CJK_END else 0.25


def split_recursive(text: str) -> list[str]:
    pieces = _atomic_pieces(text)
    return _merge_pieces(pieces, CHILD_TARGET_MAX_TOKENS)


def _atomic_pieces(text: str) -> list[str]:
    """逐级按分隔符拆到不超过 max 或无法再拆，超长不可拆片段硬切。"""
    pieces = [text]
    for sep in _SEPARATORS:
        result: list[str] = []
        for piece in pieces:
            if estimate_tokens(piece) <= CHILD_TARGET_MAX_TOKENS:
                result.append(piece)
            elif sep in piece:
                result.extend(_split_keep_separator(piece, sep))
            else:
                result.append(piece)
        pieces = result

    final: list[str] = []
    for piece in pieces:
        if estimate_tokens(piece) > CHILD_TARGET_MAX_TOKENS:
            final.extend(_hard_split(piece, CHILD_TARGET_MAX_TOKENS, CHILD_OVERLAP_TOKENS))
        else:
            final.append(piece)
    return [p for p in final if p.strip()]


def _merge_pieces(pieces: list[str], max_tokens: int) -> list[str]:
    """把原子片段贪心合并成 ~max_tokens 的块，避免逐句过碎。"""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and estimate_tokens(current + piece) > max_tokens:
            chunks.append(current.strip())
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _split_keep_separator(text: str, sep: str) -> list[str]:
    parts = text.split(sep)
    return [p + sep for p in parts[:-1]] + [parts[-1]]


def _hard_split(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """token 精确的滑动窗口硬切 + 重叠。"""
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = start
        acc = 0.0
        while end < n and acc < max_tokens:
            acc += _tokens_of_char(text[end])
            end += 1
        chunks.append(text[start:end])
        if end >= n:
            break
        # 向前回退 overlap_tokens 对应的字符数
        back = end
        back_acc = 0.0
        while back > start and back_acc < overlap_tokens:
            back -= 1
            back_acc += _tokens_of_char(text[back])
        start = back
    return [chunk for chunk in chunks if chunk.strip()]
