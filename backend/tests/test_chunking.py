"""分块层单元测试：5 splitter 边界 + parent 切分 + registry 兜底。"""

from app.chunking import chunk_document, estimate_tokens, segment_parents
from app.chunking.block import Block, BlockType, split_blocks
from app.chunking.registry import get_splitter, split_children
from app.chunking.splitters import (
    split_code,
    split_faq,
    split_legal,
    split_recursive,
    split_table,
)


def test_recursive_short_returns_single() -> None:
    assert split_recursive("这是一段很短的文本。") == ["这是一段很短的文本。"]


def test_recursive_splits_long_paragraph_by_sentence() -> None:
    text = "这是第一句话，用来测试切分。" * 100
    chunks = split_recursive(text)
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 500 for chunk in chunks)


def test_recursive_hard_split_with_overlap() -> None:
    # 无分隔符的超长串 → 硬切 + 重叠
    text = "字" * 5000
    chunks = split_recursive(text)
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)
    # 相邻 chunk 有重叠（前一块结尾 == 后一块开头）
    assert chunks[0][-100:] == chunks[1][:100]


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


def test_legal_split_by_clause() -> None:
    text = "第一条 总则\n这是第一条内容。\n第二条 定义\n这是第二条内容。\n第三条 附则\n这是第三条。"
    chunks = split_legal(text)
    assert len(chunks) == 3
    assert chunks[0].startswith("第一条")
    assert chunks[1].startswith("第二条")


def test_faq_split_by_qa_pair() -> None:
    text = "Q: 如何安装？\nA: 运行 pip install。\nQ: 如何卸载？\nA: 运行 pip uninstall。"
    chunks = split_faq(text)
    assert len(chunks) == 2
    assert "问：如何安装？" in chunks[0]
    assert "答：运行 pip install。" in chunks[0]


def test_block_split_identifies_types() -> None:
    markdown = (
        "# 标题\n\n"
        "段落内容。\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "```python\nprint(1)\n```\n\n"
        "- 项目一\n- 项目二\n"
    )
    blocks = split_blocks(markdown)
    types = [b.type for b in blocks]
    assert BlockType.HEADING in types
    assert BlockType.PARAGRAPH in types
    assert BlockType.TABLE in types
    assert BlockType.CODE in types
    assert BlockType.LIST in types


def test_parent_heading_sections() -> None:
    body = "内容" * 600  # 每节约 1200 token，避免触发合并
    markdown = f"# 第一节\n\n{body}\n\n# 第二节\n\n{body}\n"
    parents = segment_parents(markdown)
    assert len(parents) == 2
    assert parents[0].heading_path == "第一节"
    assert parents[1].heading_path == "第二节"


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


def test_registry_falls_back_to_recursive() -> None:
    splitter = get_splitter(BlockType.PARAGRAPH)
    assert splitter is split_recursive


def test_chunk_document_two_level_structure() -> None:
    body = "内容" * 600
    markdown = f"# 概述\n\n{body}\n\n# 细节\n\n{body}\n"
    parents = chunk_document(markdown)
    assert len(parents) >= 2
    total_children = sum(len(parent.children) for parent in parents)
    assert total_children >= 2
    # parent 有 heading_path，child 有 block_type + heading_path
    assert parents[0].heading_path == "概述"
    assert all(parent.children for parent in parents)


def test_split_children_attaches_metadata() -> None:
    block = Block(BlockType.PARAGRAPH, "正文内容。")
    chunks = split_children([block], "第一章")
    assert chunks[0].metadata["block_type"] == "paragraph"
    assert chunks[0].metadata["heading_path"] == "第一章"
