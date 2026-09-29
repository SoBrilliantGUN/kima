"""自定义 Skill（L2 技能层）文件存储：frontmatter 解析 + 目录只读加载。"""

from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from app.agent.runtime.context import ContextConfig
from app.agent.runtime.reactive import _build_invoked_skills_block, build_reactive_graph
from app.core.skill_store import FileSkillStore, _parse_skill
from tests.fakes import FakeOutputReviewer


def test_parse_skill_with_frontmatter() -> None:
    text = "---\nname: 写周报\ndescription: 写周报用这个模板\n---\n\n# 正文\n按这个结构写。\n"
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


def test_build_invoked_skills_block_empty() -> None:
    assert _build_invoked_skills_block({}, ContextConfig().skills_budget) == ""


def test_build_invoked_skills_block_formats() -> None:
    skills = {"写周报": "1. 完成\n2. 计划"}
    block = _build_invoked_skills_block(skills, ContextConfig().skills_budget)
    assert block.startswith("[INVOKED SKILLS]\n")
    assert "### Skill: 写周报" in block
    assert "1. 完成" in block


def test_build_invoked_skills_block_truncates_to_budget() -> None:
    """L3 超预算截断：逐条估算，塞不下的整条丢弃（文档 §2.1「超 25k 截断」）。"""
    skills = {"skill_a": "a" * 1000, "skill_b": "b" * 1000}
    # 极小预算只放得下第一条（含前缀/分隔符），第二条被丢弃
    block = _build_invoked_skills_block(skills, skills_budget=50)
    assert "### Skill: skill_a" in block
    assert "skill_b" not in block


class _RecordingModel(FakeMessagesListChatModel):
    """记录每次模型调用收到的消息列表。"""

    received: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    def bind_tools(self, tools: object, **kwargs: object) -> "_RecordingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_invoked_skills_block_injected_into_agent_input() -> None:
    """L3 [INVOKED SKILLS] 块：get_skill 加载的全文每轮拼进 agent 输入（skills 在 state 前）。"""
    model = _RecordingModel(responses=[AIMessage(content="done")])
    graph = build_reactive_graph(
        model, [], reviewer=FakeOutputReviewer(), invoked_skills={"写周报": "1. 完成\n2. 计划"}
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "system_prompt": "system",
        "memory_block": "",
        "reminder": "",
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    async for _ in graph.astream(initial, stream_mode="updates"):
        pass
    assert len(model.received) == 1
    contents = [str(m.content) for m in model.received[0]]
    skills_idx = next(i for i, c in enumerate(contents) if c.startswith("[INVOKED SKILLS]"))
    state_idx = next(i for i, c in enumerate(contents) if c.startswith("[STATE]"))
    assert skills_idx < state_idx  # skills 在 state 之前
    assert "写周报" in contents[skills_idx]
