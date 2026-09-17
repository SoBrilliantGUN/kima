"""FAQ splitter：按「问答对」切分（Q/A 或 问/答）。"""

import re

_FAQ_Q_RE = re.compile(r"^\s*(?:Q|问)\s*[:：]\s*(.*)$", re.IGNORECASE)
_FAQ_A_RE = re.compile(r"^\s*(?:A|答)\s*[:：]\s*(.*)$", re.IGNORECASE)


def split_faq(content: str) -> list[str]:
    pairs: list[tuple[str, str]] = []
    question: str | None = None
    answers: list[str] = []

    for line in content.strip().split("\n"):
        match_q = _FAQ_Q_RE.match(line)
        match_a = _FAQ_A_RE.match(line)
        if match_q:
            if question is not None:
                pairs.append((question, "\n".join(answers)))
            question = match_q.group(1).strip()
            answers = []
        elif match_a and question is not None:
            answers.append(match_a.group(1).strip())
        elif question is not None:
            answers.append(line.strip())
    if question is not None:
        pairs.append((question, "\n".join(answers)))

    chunks: list[str] = []
    for q, a in pairs:
        text = f"问：{q}"
        if a:
            text += f"\n答：{a}"
        chunks.append(text)
    return chunks
