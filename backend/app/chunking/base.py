"""分块基础：目标长度常量、token 估算、Chunk/ParentChunk 数据类、递归切分。

递归切分（段落→行→句→词逐级拆 + 贪心合并 + 相邻块重叠）是父级与子级共用的
兜底原语，仅目标大小不同（父级 ~1500、子级 ~500）。父级按标题层级切「节」见
`block.py` 的 `segment_parents`；无标题文档或超大节回退为递归切分；过小节合并，
目标每 parent ~1000–2000 token。
子级切分（content-aware）见 `block.py` + `registry.py` + `splitters/`。
"""

from dataclasses import dataclass, field

# 目标长度（token 估算值，非精确 tokenizer）
CHILD_TARGET_MAX_TOKENS = 500  # child 目标 ~300–500 的上界
CHUNK_OVERLAP_TOKENS = 50  # 递归切分相邻块的 token 重叠量
PARENT_TARGET_MIN_TOKENS = 1000
PARENT_TARGET_MAX_TOKENS = 2000
PARENT_FALLBACK_WINDOW_TOKENS = 1500

# CJK 统一码基本区：CJK 字符约 1 token/字，其余约 4 字符/token
_CJK_START = "一"
_CJK_END = "鿿"


def _is_cjk(ch: str) -> bool:
    """是否为 CJK 字符（统一码基本区）。"""
    return _CJK_START <= ch <= _CJK_END


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符约 1 token/字，其余按 4 字符/token。

    仅用于分块长度判断与 `token_count` 落库，不需要精确 tokenizer。
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
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


# 递归切分分隔符优先级：段落 → 行 → 中文句 → 英文句 → 词
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", " "]


def _tokens_of_char(ch: str) -> float:
    """单字符 token 估算：CJK 约 1，其余约 1/4。"""
    return 1.0 if _is_cjk(ch) else 0.25


def split_recursive(text: str, max_tokens: int = CHILD_TARGET_MAX_TOKENS) -> list[str]:
    """结构化递归切分：段落→行→句→词逐级拆原子片段，再贪心合并回 ~`max_tokens`。

    父级（~PARENT_FALLBACK_WINDOW_TOKENS）与子级（~CHILD_TARGET_MAX_TOKENS）共用，
    仅目标大小不同；无法再拆的超长片段用 token 精确硬切 + 重叠兜底。
    相邻合并块之间保留 ~CHUNK_OVERLAP_TOKENS 的重叠（句子边界对齐），避免边界语义腰斩。
    """
    pieces = _atomic_pieces(text, max_tokens)
    return _merge_pieces(pieces, max_tokens)


def _atomic_pieces(text: str, max_tokens: int) -> list[str]:
    """逐级按分隔符拆到不超过 max 或无法再拆，超长不可拆片段硬切。"""
    pieces = [text]
    for sep in _SEPARATORS:
        result: list[str] = []
        for piece in pieces:
            if estimate_tokens(piece) <= max_tokens:
                result.append(piece)
            elif sep in piece:
                result.extend(_split_keep_separator(piece, sep))
            else:
                result.append(piece)
        pieces = result

    final: list[str] = []
    for piece in pieces:
        if estimate_tokens(piece) > max_tokens:
            final.extend(_hard_split(piece, max_tokens, CHUNK_OVERLAP_TOKENS))
        else:
            final.append(piece)
    return [p for p in final if p.strip()]


def _merge_pieces(
    pieces: list[str],
    max_tokens: int,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """把原子片段贪心合并成 ~max_tokens 的块，相邻块保留 ~overlap_tokens 重叠。

    重叠只取上一块尾部、且不使新块越界（尾部 + 首个片段 <= max 才携带）；超长
    片段本身已由 `_hard_split` 携带重叠，此处不再重复。
    """
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        piece_tokens = estimate_tokens(piece)
        if current and current_tokens + piece_tokens > max_tokens:
            chunks.append("".join(current).strip())
            tail = _tail_pieces(current, overlap_tokens)
            tail_tokens = estimate_tokens("".join(tail))
            if tail_tokens + piece_tokens <= max_tokens:
                current = tail + [piece]
                current_tokens = tail_tokens + piece_tokens
            else:
                current = [piece]
                current_tokens = piece_tokens
        else:
            current.append(piece)
            current_tokens += piece_tokens
    if current:
        chunks.append("".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _tail_pieces(current: list[str], overlap_tokens: int) -> list[str]:
    """从 `current` 尾部取若干原子片段，累计 ~overlap_tokens（至少取一个）。"""
    tail: list[str] = []
    acc = 0
    for piece in reversed(current):
        piece_tokens = estimate_tokens(piece)
        if tail and acc + piece_tokens > overlap_tokens:
            break
        tail.append(piece)
        acc += piece_tokens
    return list(reversed(tail))


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


def split_by_lines(text: str, max_tokens: int) -> list[str]:
    """按行累加切分，保证每段不超过 `max_tokens`（供代码等行结构内容兜底）。"""
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
