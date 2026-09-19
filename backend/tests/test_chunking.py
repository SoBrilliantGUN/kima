"""分块层单元测试：3 splitter 边界 + parent 切分 + AST 语义块 + registry 兜底。"""

from app.chunking import chunk_document, estimate_tokens, segment_parents, split_blocks
from app.chunking.base import split_recursive
from app.chunking.block import Block, BlockType
from app.chunking.registry import get_splitter, split_children
from app.chunking.splitters import split_code, split_table


def _overlap_len(a: str, b: str) -> int:
    """a 的尾缀与 b 的前缀的最长公共长度（相邻块重叠量）。"""
    n = min(len(a), len(b))
    for k in range(n, 0, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


# --- 递归切分 ---


def test_recursive_short_returns_single() -> None:
    assert split_recursive("这是一段很短的文本。") == ["这是一段很短的文本。"]


def test_recursive_splits_long_paragraph_by_sentence() -> None:
    text = "这是第一句话，用来测试切分。" * 100
    chunks = split_recursive(text)
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 500 for chunk in chunks)


def test_recursive_merge_keeps_overlap_between_chunks() -> None:
    # 可区分句子：合并后的相邻块应在句子边界保留 ~50 token 重叠
    sentences = [f"句子{i:03d}这是第{i}句话。" for i in range(100)]
    chunks = split_recursive("".join(sentences))
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 500 for chunk in chunks)
    assert _overlap_len(chunks[0], chunks[1]) > 0


def test_recursive_hard_split_with_overlap() -> None:
    # 无分隔符的超长串 → 硬切 + 重叠（用可区分文本验证，而非全同字符）
    text = "".join(chr(0x4E00 + (i % 100)) for i in range(5000))
    chunks = split_recursive(text)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)
    assert _overlap_len(chunks[0], chunks[1]) > 0


# --- 专用 splitter ---


def test_table_small_kept_whole() -> None:
    table = "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 10 |"
    assert split_table(table) == [table]


def test_table_large_split_with_header() -> None:
    header = "| 列 | 值 |\n| --- | --- |"
    rows = [f"| 行{i} | {'x' * 200} |" for i in range(50)]
    table = header + "\n" + "\n".join(rows)
    chunks = split_table(table)
    assert len(chunks) > 1
    for chunk in chunks:
        lines = chunk.split("\n")
        assert lines[0] == "| 列 | 值 |"
        assert lines[1] == "| --- | --- |"


def test_code_split_by_function_boundaries() -> None:
    funcs = []
    for i in range(30):
        body = "\n".join(f"    x = {j}" for j in range(20))
        funcs.append(f"def func_{i}():\n{body}")
    code = "```python\n" + "\n\n".join(funcs) + "\n```"
    chunks = split_code(code)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.split("\n")[0].startswith("def func_")


def test_code_no_boundary_falls_back_to_lines() -> None:
    code = "```\n" + "\n".join(f"x = {i}" for i in range(100)) + "\n```"
    chunks = split_code(code)
    assert len(chunks) >= 1


# --- AST 语义块 ---


def test_block_split_code_fence_language() -> None:
    # 非缩进与缩进的代码围栏都应正确提取语言标识
    markdown = "```python\nprint(1)\n```\n\n   ```rust\nfn main() {}\n   ```"
    blocks = split_blocks(markdown)
    code = [b for b in blocks if b.type == BlockType.CODE]
    assert [b.metadata["language"] for b in code] == ["python", "rust"]


def test_block_split_identifies_types() -> None:
    markdown = (
        "# 标题\n\n"
        "段落内容。\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "```python\nprint(1)\n```\n\n"
        "- 项目一\n- 项目二\n\n"
        "> 引用内容\n"
    )
    blocks = split_blocks(markdown)
    types = [b.type for b in blocks]
    for expected in (
        BlockType.HEADING,
        BlockType.PARAGRAPH,
        BlockType.TABLE,
        BlockType.CODE,
        BlockType.LIST,
        BlockType.BLOCKQUOTE,
    ):
        assert expected in types


def test_block_carries_heading_path() -> None:
    markdown = "# 第一章\n\n## 1.1\n\n正文。\n"
    blocks = split_blocks(markdown)
    paragraph = next(b for b in blocks if b.type == BlockType.PARAGRAPH)
    assert paragraph.metadata["heading_path"] == "第一章 > 1.1"


# --- parent 切分 ---


def test_parent_heading_sections() -> None:
    body = "内容" * 600  # 每节约 1200 token，避免触发合并
    markdown = f"# 第一节\n\n{body}\n\n# 第二节\n\n{body}\n"
    parents = segment_parents(markdown)
    assert len(parents) == 2
    assert parents[0].heading_path == "第一节"
    assert parents[1].heading_path == "第二节"


def test_parent_single_title_does_not_collapse() -> None:
    # 一个 # 标题 + 多个 ## 节：不应塌成一个 parent（旧 bug）
    body = "内容" * 700  # ~1400 token，每节 > 1000 避免合并
    markdown = f"# 标题\n\n## 第一节\n\n{body}\n\n## 第二节\n\n{body}\n"
    parents = segment_parents(markdown)
    assert len(parents) == 2
    assert parents[0].heading_path == "标题 > 第一节"
    assert parents[1].heading_path == "标题 > 第二节"


def test_parent_no_heading_window_fallback() -> None:
    markdown = "\n\n".join(f"段落{i}。" for i in range(20))
    parents = segment_parents(markdown)
    assert len(parents) >= 1
    assert all(parent.heading_path == "" for parent in parents)


def test_parent_merge_small_sections() -> None:
    markdown = "# 一\n\n短。\n\n# 二\n\n短。\n"
    parents = segment_parents(markdown)
    # 两节都很小，会合并为一节
    assert len(parents) == 1


def test_parent_split_oversized_section() -> None:
    markdown = "# 大节\n\n" + "\n\n".join("内容" * 50 for _ in range(30))
    parents = segment_parents(markdown)
    assert len(parents) > 1


# --- registry / 两级管线 ---


def test_registry_falls_back_to_recursive() -> None:
    splitter = get_splitter(BlockType.PARAGRAPH)
    assert splitter is split_recursive


def test_split_children_attaches_metadata() -> None:
    block = Block(BlockType.PARAGRAPH, "正文内容。", {"heading_path": "第一章"})
    chunks = split_children([block])
    assert chunks[0].metadata["block_type"] == "paragraph"
    assert chunks[0].metadata["heading_path"] == "第一章"


def test_heading_emitted_as_child() -> None:
    # 标题应作为可检索 child 落地（而非仅 metadata）
    parents = chunk_document("# 支付流程\n\n这是正文。\n")
    headings = [c for c in parents[0].children if c.metadata["block_type"] == "heading"]
    assert headings and headings[0].content == "支付流程"


def test_chunk_document_two_level_structure() -> None:
    body = "内容" * 600
    markdown = f"# 概述\n\n{body}\n\n# 细节\n\n{body}\n"
    parents = chunk_document(markdown)
    assert len(parents) >= 2
    total_children = sum(len(parent.children) for parent in parents)
    assert total_children >= 2
    assert parents[0].heading_path == "概述"
    assert all(parent.children for parent in parents)
