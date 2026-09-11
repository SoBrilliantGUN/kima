import hashlib
from typing import Protocol


class EmbeddingClient(Protocol):
    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class FakeEmbeddingClient:
    """确定性伪向量：对文本 hash 生成 [-1, 1] 区间、长度等于 dimension 的向量。"""

    def __init__(self, dimension: int = 1024) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [
            (digest[i % len(digest)] / 255.0) * 2.0 - 1.0 for i in range(self._dimension)
        ]
        self._validate(vector)
        return vector

    def _validate(self, vector: list[float]) -> None:
        if len(vector) != self._dimension:
            raise ValueError(
                f"Embedding dimension mismatch: got {len(vector)}, expected {self._dimension}"
            )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
