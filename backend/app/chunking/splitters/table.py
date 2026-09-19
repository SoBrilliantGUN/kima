"""表格 splitter：整表保留；超长按行切 + 重复表头。"""

from app.chunking.base import CHILD_TARGET_MAX_TOKENS, estimate_tokens


def split_table(content: str) -> list[str]:
    """按行切分超长表格，每个分块重复表头以保持可读性。

    表头（首行）+ 分隔行（`|---|`）固定作为每个 chunk 的前缀，正文行贪心累加，
    超过 `CHILD_TARGET_MAX_TOKENS` 时落一个 chunk 并重新以表头开头。
    整表不超长（或不足 3 行、无法识别表头）时原样整表返回。
    """
    # 过滤空行；markdown 表格需 >= 3 行（表头 + 分隔行 + 至少一行正文）。
    lines = [line for line in content.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return [content.strip()]
    header = lines[:2]  # 表头 + `|---|` 分隔行
    body = lines[2:]

    if estimate_tokens("\n".join(lines)) <= CHILD_TARGET_MAX_TOKENS:
        return [content.strip()]

    chunks: list[str] = []
    current = list(header)
    for row in body:
        candidate = current + [row]
        # 已有正文行且再添一行会超长 → 落当前 chunk，新 chunk 从表头 + 该行重新开始。
        if current != header and estimate_tokens("\n".join(candidate)) > CHILD_TARGET_MAX_TOKENS:
            chunks.append("\n".join(current))
            current = list(header) + [row]
        else:
            current = candidate
    # 收尾：当前 chunk 含正文行时落地，避免产出只含表头的空 chunk。
    if len(current) > len(header):
        chunks.append("\n".join(current))
    return chunks
