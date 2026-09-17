import hashlib
from typing import Protocol

import httpx


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


class SiliconFlowEmbeddingClient:
    """SiliconFlow（OpenAI 兼容）`/embeddings` 实现，model=bge-m3。

    调用方按 batch_size 分批调用 `embed_documents`；单批失败重试一次，仍失败抛出，
    由 ingest 捕获后判定文档 error。返回前断言 `len(vec) == dimension` 防配置漂移。
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, dimension: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed_with_retry(texts)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_with_retry([text])
        return vectors[0]

    async def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        try:
            return await self._embed(texts)
        except Exception:
            # 单批失败重试一次
            return await self._embed(texts)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": texts}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/embeddings", json=payload, headers=headers
            )
        response.raise_for_status()

        data = response.json()
        items = sorted(data["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in items]
        for vector in vectors:
            if len(vector) != self._dimension:
                raise ValueError(
                    f"Embedding dimension mismatch: got {len(vector)}, expected {self._dimension}"
                )
        return vectors
