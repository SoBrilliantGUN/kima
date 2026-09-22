"""Soul/User 记忆文件存储 + Copilot 形象资料：读写 + 幂等初始化。"""

from pathlib import Path

from app.core.memory_store import FileMemoryStore


async def test_ensure_creates_both_files(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path)
    await store.ensure()
    assert (tmp_path / "soul.md").exists()
    assert (tmp_path / "user.md").exists()


async def test_ensure_is_idempotent(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path)
    await store.ensure()
    await store.write("soul", "简洁回答")
    await store.ensure()  # 再次初始化不应覆盖已有内容
    assert await store.read("soul") == "简洁回答"


async def test_write_and_read(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path)
    await store.write("user", "用户是副总经理")
    assert await store.read("user") == "用户是副总经理"
    assert await store.read("soul") == ""


async def test_read_missing_returns_empty(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path)
    assert await store.read("soul") == ""
