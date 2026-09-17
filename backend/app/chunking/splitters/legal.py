"""法律条文 splitter：按「第 X 条/款/项」边界切分。"""

import re

from app.chunking.splitters.recursive import split_recursive

_LEGAL_RE = re.compile(r"^第\s*[一二三四五六七八九十百千万零〇\d]+\s*[条款项]")


def split_legal(content: str) -> list[str]:
    lines = [line for line in content.strip().split("\n") if line.strip()]
    indices = [i for i, line in enumerate(lines) if _LEGAL_RE.match(line)]
    if not indices:
        return split_recursive(content)

    # 第一个条文前的前言并入首条
    chunks: list[str] = []
    preamble = lines[: indices[0]]
    for idx, start in enumerate(indices):
        end = indices[idx + 1] if idx + 1 < len(indices) else len(lines)
        chunk_lines = lines[start:end]
        if idx == 0 and preamble:
            chunk_lines = preamble + chunk_lines
        chunk = "\n".join(chunk_lines).strip()
        if chunk:
            chunks.append(chunk)
    return chunks
