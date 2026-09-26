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
    skills, total = await store.list_skills()
    assert total == 2
    assert [s.name for s in skills] == ["a", "b"]
    assert skills[0].description == "技能 a"
    assert skills[1].description == ""
    assert skills[1].content == "正文 b（无 frontmatter）"


async def test_file_skill_store_missing_dir(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path / "nope")
    skills, total = await store.list_skills()
    assert skills == []
    assert total == 0


async def test_write_and_read_skill(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path)
    await store.write_skill("写周报", "写周报用这个模板", "1. 本周完成\n2. 下周计划")
    skill = await store.read_skill("写周报")
    assert skill is not None
    assert skill.name == "写周报"
    assert skill.description == "写周报用这个模板"
    assert skill.content == "1. 本周完成\n2. 下周计划"
    skills, _ = await store.list_skills()
    assert [s.name for s in skills] == ["写周报"]


async def test_write_skill_upserts_by_name(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path)
    await store.write_skill("a", "d1", "c1")
    await store.write_skill("a", "d2", "c2")
    skills, _ = await store.list_skills()
    assert len(skills) == 1  # 同名 upsert 不新增
    assert skills[0].description == "d2"
    assert skills[0].content == "c2"


async def test_delete_skill(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path)
    await store.write_skill("a", "d", "c")
    assert await store.delete_skill("a") is True
    assert await store.read_skill("a") is None
    assert await store.delete_skill("a") is False  # 已删，再次删返回 False


async def test_write_skill_updates_user_dropped_file_by_name(tmp_path: Path) -> None:
    """upsert 按 frontmatter name 解析既有文件：覆盖手工直放、文件名与 name 不一致的文件。"""
    (tmp_path / "weekly.md").write_text(
        "---\nname: 写周报\ndescription: 旧描述\n---\n旧正文\n", encoding="utf-8"
    )
    store = FileSkillStore(tmp_path)
    await store.write_skill("写周报", "新描述", "新正文")
    skills, _ = await store.list_skills()
    assert len(skills) == 1  # 覆盖既有文件，不新增
    assert skills[0].name == "写周报"
    assert skills[0].description == "新描述"
    assert skills[0].content == "新正文"


async def test_list_skills_paginates(tmp_path: Path) -> None:
    store = FileSkillStore(tmp_path)
    for i in range(5):
        await store.write_skill(f"skill{i}", f"d{i}", f"c{i}")
    page, total = await store.list_skills(limit=2, offset=0)
    assert total == 5
    assert [s.name for s in page] == ["skill0", "skill1"]
    page2, _ = await store.list_skills(limit=2, offset=2)
    assert [s.name for s in page2] == ["skill2", "skill3"]
    all_, _ = await store.list_skills()
    assert len(all_) == 5
