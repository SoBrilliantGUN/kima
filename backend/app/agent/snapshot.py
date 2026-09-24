"""调用级快照：把每次 LLM 调用当纯函数，输入内容哈希为键，成功后落 output。

对照 ``docs/llm-gateway.md`` 决策 D3/D4：
- 快照粒度 = 调用级（每次 LLM 调用的输入 + 输出）；
- 重放语义 = at-least-once：已成功落快照的调用按 ``(run_id, call_key)`` 复用、绝不复跑；
  「已成功但未落快照」的崩溃窗口会重放（多付一次，可接受）。

本模块只定义抽象契约与内存实现；Postgres 实现见 ``app/repositories/llm_snapshot.py``。
快照是「本 run 的重放缓存」而非「全局内容缓存」——以 ``run_id`` 划界，避免跨 run 的
temperature>0 输出被错误复用。
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMSnapshotRecord:
    """一条已落库的快照记录（供测试/审计读取）。"""

    run_id: str
    call_key: str
    kind: str
    output: dict[str, Any]
    usage: dict[str, Any] | None


class SnapshotStore(Protocol):
    """快照存取原语（可注入内存 Fake 做确定性测试）。

    ``get`` 返回已成功调用的 output 字典（未命中返回 None）；``put`` 幂等覆盖写。
    ``run_id`` 是 thread_id 字符串，``call_key`` 是「node + 规范化输入」的内容哈希。
    """

    async def get(self, run_id: str, call_key: str) -> dict[str, Any] | None: ...

    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        kind: str,
        output: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> None: ...


class InMemorySnapshotStore:
    """内存版快照（测试/无 DB 场景）：dict 幂等覆盖写。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], LLMSnapshotRecord] = {}

    async def get(self, run_id: str, call_key: str) -> dict[str, Any] | None:
        record = self._rows.get((run_id, call_key))
        return record.output if record is not None else None

    async def put(
        self,
        run_id: str,
        call_key: str,
        *,
        kind: str,
        output: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> None:
        self._rows[(run_id, call_key)] = LLMSnapshotRecord(
            run_id=run_id, call_key=call_key, kind=kind, output=output, usage=usage
        )
