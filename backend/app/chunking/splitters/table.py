"""表格 splitter：整表保留；超长按行切 + 重复表头。"""

from app.chunking.base import CHILD_TARGET_MAX_TOKENS, estimate_tokens


def split_table(content: str) -> list[str]:
    lines = [line for line in content.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return [content.strip()]
    header = lines[:2]
    body = lines[2:]

    if estimate_tokens("\n".join(lines)) <= CHILD_TARGET_MAX_TOKENS:
        return [content.strip()]

    chunks: list[str] = []
    current = list(header)
    for row in body:
        candidate = current + [row]
        if current != header and estimate_tokens("\n".join(candidate)) > CHILD_TARGET_MAX_TOKENS:
            chunks.append("\n".join(current))
            current = list(header) + [row]
        else:
            current = candidate
    if len(current) > len(header):
        chunks.append("\n".join(current))
    return chunks
