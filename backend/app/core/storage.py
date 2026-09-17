"""文件存储抽象（FileStore）。

默认本地磁盘 `backend/storage/documents/`（gitignore），`documents.file_path` 存相对路径；
后续换对象存储只需替换 FileStore 实现，业务层不动。
"""

import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import BACKEND_DIR

DOCUMENTS_DIR = BACKEND_DIR / "storage" / "documents"


class FileStore(Protocol):
    async def save(self, content: bytes, ext: str) -> str: ...
    async def open(self, path: str) -> bytes: ...
    async def delete(self, path: str) -> None: ...


class LocalFileStore:
    """本地磁盘实现：文件按 uuid 命名落盘，返回相对路径。"""

    def __init__(self, base_dir: Path = DOCUMENTS_DIR) -> None:
        self._base_dir = base_dir

    async def save(self, content: bytes, ext: str) -> str:
        name = f"{uuid.uuid4().hex}.{ext}"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        (self._base_dir / name).write_bytes(content)
        return name

    async def open(self, path: str) -> bytes:
        return (self._base_dir / path).read_bytes()

    async def delete(self, path: str) -> None:
        target = self._base_dir / path
        if target.exists():
            target.unlink()
