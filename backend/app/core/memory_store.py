"""Soul / User 记忆的文件存储（文件，不入库）。

- Soul（人设/说话风格）与 User（档案/背景/偏好）是稳定短文本，每轮全文注入，
  以 `data/memory/soul.md` / `user.md` 两个文件承载，覆盖写、幂等初始化。
- L1 积累层（情节/事实/偏好）走向量表 `copilot_memories`，不进本模块。
"""

import os
import tempfile
from pathlib import Path
from typing import Protocol

from app.core.config import BACKEND_DIR

MEMORY_DIR = BACKEND_DIR / "data" / "memory"

# 允许覆盖写的稳定记忆文件（不含三型积累记忆）
_SOUL_NAME = "soul"
_USER_NAME = "user"
_PROFILE_NAMES = (_SOUL_NAME, _USER_NAME)


class MemoryFileStore(Protocol):
    async def read(self, name: str) -> str: ...
    async def write(self, name: str, content: str) -> None: ...
    async def ensure(self) -> None: ...


class FileMemoryStore:
    """本地磁盘实现：`soul.md` / `user.md` 两个文件。"""

    def __init__(self, base_dir: Path = MEMORY_DIR) -> None:
        self._base_dir = base_dir

    def _path(self, name: str) -> Path:
        return self._base_dir / f"{name}.md"

    async def ensure(self) -> None:
        """幂等初始化：目录与两个文件缺失时创建空文件。"""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        for name in _PROFILE_NAMES:
            path = self._path(name)
            if not path.exists():
                path.write_text("", encoding="utf-8")

    async def read(self, name: str) -> str:
        path = self._path(name)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def write(self, name: str, content: str) -> None:
        """原子覆盖写：临时文件 + ``os.replace``（写一半崩溃/并发读不会看到半截文件）。

        直接 ``write_text`` 是「先截断再写」，并发读写会读到空/半截内容（《隔离优于共享》
        场景一的进度文件竞态）；改为同目录临时文件写完后原子替换。
        """
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=self._base_dir, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
