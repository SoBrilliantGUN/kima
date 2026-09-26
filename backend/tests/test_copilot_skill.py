"""自定义 Skill（L2 技能层）文件存储：frontmatter 解析 + 目录只读加载。"""

from pathlib import Path

from app.core.skill_store import FileSkillStore, _parse_skill


def test_parse_skill_with_frontmatter() -> None:
    text = (
        "---\n"
        "name: 写周报\n"
        "description: 写周报用这个模板\n"
        "---\n"
        "\n"
        "# 正文\n"
        "按这个结构写。\n"
    )
    skill = _parse_skill("fallback", text)
    assert skill.name == "写周报"
    assert skill.description == "写周报用这个模板"
    assert skill.content == "# 正文\n按这个结构写。"


def test_parse_skill_no_frontmatter_falls_back() -> None:
    skill = _parse_skill("note", "直接是正文，没有 frontmatter。")
    assert skill.name == "note"
    assert skill.description == ""
    assert skill.content == "直接是正文，没有 frontmatter。"


async def test_file_skill_store_lists_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: 技能 a\n---\n正文 a\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("正文 b（无 frontmatter）\n", encoding="utf-8")
    store = FileSkillStore(tmp_path)
    skills = await store.list_skills()
    assert [s.name for s in skills] == ["a", "b"]
    assert skills[0].description == "技能 a"
    assert skills[1].description == ""
    assert skills[1].content == "正文 b（无 frontmatter）"


async def test_file_skill_store_missing_dir(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path / "nope")
    assert await store.list_skills() == []
