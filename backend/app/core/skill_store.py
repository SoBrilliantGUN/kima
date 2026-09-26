"""自定义 Skill（L2 技能层）的文件存储：一个 skill = 一个 MD 文件。

- Agent 可经 `write_skill`/`delete_skill` 工具
  增删改（见 module-6 §2.6）。**官方内置工具（`/api/copilot/skills`）是代码、不可删**，
  本存储只操作 `data/skills/*.md` 的「用户安装」skill。
- 文件格式：frontmatter（``name`` + ``description``）+ 正文（skill 的指令/经验 Markdown）。
  无 frontmatter 时以文件名为 name、description 空、全文为正文。
- skill 的**身份 = frontmatter ``name``**（无 frontmatter 时退化到文件名 stem）；
  写文件用 ``_slug(name).md`` 命名，读/删按 name 解析既有文件（兼容手工直放的文件名）。
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import BACKEND_DIR

SKILL_DIR = BACKEND_DIR / "data" / "skills"

# 匹配 ``---\n<frontmatter>\n---\n<body>``；body 可空。
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n(.*))?\Z", re.DOTALL
)
# 文件名安全化：非「Unicode 词字符/连字符」替换为下划线（保留中文/字母/数字/_/-）。
_SLUG_RE = re.compile(r"[^\w\-]", re.UNICODE)


@dataclass(frozen=True)
class CustomSkill:
    """一个自定义 skill：name 唯一标识、description 一句话、content 正文。"""

    name: str
    description: str
    content: str


class SkillFileStore(Protocol):
    async def list_skills(
        self, limit: int | None = None, offset: int = 0
    ) -> tuple[list[CustomSkill], int]: ...
    async def read_skill(self, name: str) -> CustomSkill | None: ...
    async def write_skill(self, name: str, description: str, content: str) -> CustomSkill: ...
    async def delete_skill(self, name: str) -> bool: ...


class FileSkillStore:
    """本地磁盘实现：`data/skills/*.md`（list/read/write/delete）。"""

    def __init__(self, base_dir: Path = SKILL_DIR) -> None:
        self._base_dir = base_dir

    async def list_skills(
        self, limit: int | None = None, offset: int = 0
    ) -> tuple[list[CustomSkill], int]:
        """分页列出：返回 (本页 skills, 总数)。``limit=None`` 取全部（offset 起）。

        目录 listing（``glob``）拿全部文件名算 total 很便宜，正文只在取本页时才读/解析，
        避免几百个 skill 时每次都全量读盘。
        """
        if not self._base_dir.is_dir():
            return [], 0
        paths = sorted(self._base_dir.glob("*.md"))
        total = len(paths)
        page = paths[offset:] if limit is None else paths[offset : offset + limit]
        skills = [_parse_skill(p.stem, p.read_text(encoding="utf-8")) for p in page]
        return skills, total

    async def read_skill(self, name: str) -> CustomSkill | None:
        path = self._find_path(name)
        if path is None:
            return None
        return _parse_skill(path.stem, path.read_text(encoding="utf-8"))

    async def write_skill(self, name: str, description: str, content: str) -> CustomSkill:
        """upsert：按 name 命中既有文件则覆盖，否则新建 ``_slug(name).md``。原子写。"""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._find_path(name) or self._base_dir / f"{_slug(name)}.md"
        text = _format_skill(name, description, content)
        fd, tmp = tempfile.mkstemp(dir=self._base_dir, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return CustomSkill(name=name, description=description, content=content)

    async def delete_skill(self, name: str) -> bool:
        path = self._find_path(name)
        if path is None:
            return False
        path.unlink()
        return True

    def _find_path(self, name: str) -> Path | None:
        """按 name 解析文件：先匹配文件名 stem，再匹配 frontmatter name（兼容手工直放）。"""
        if not self._base_dir.is_dir():
            return None
        for path in sorted(self._base_dir.glob("*.md")):
            if path.stem == name or path.stem == _slug(name):
                return path
        for path in sorted(self._base_dir.glob("*.md")):
            skill = _parse_skill(path.stem, path.read_text(encoding="utf-8"))
            if skill.name == name:
                return path
        return None


def _slug(name: str) -> str:
    """把 skill name 转成文件名安全的 stem（保留中文/字母/数字/_/-）。"""
    return _SLUG_RE.sub("_", name).strip("_") or "skill"


def _format_skill(name: str, description: str, content: str) -> str:
    """序列化一个 skill 文件：frontmatter + 正文。"""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{content}\n"


def _parse_skill(default_name: str, text: str) -> CustomSkill:
    """解析一个 skill 文件：frontmatter name/description + 正文；无 frontmatter 回退。"""
    match = _FRONTMATTER_RE.match(text.strip())
    if match is None:
        return CustomSkill(name=default_name, description="", content=text.strip())
    front, body = match.group(1), (match.group(2) or "").strip()
    name, description = default_name, ""
    for raw in front.splitlines():
        line = raw.strip()
        if line.startswith("name:"):
            name = line[len("name:") :].strip().strip("\"'") or default_name
        elif line.startswith("description:"):
            description = line[len("description:") :].strip().strip("\"'")
    return CustomSkill(name=name, description=description, content=body)
