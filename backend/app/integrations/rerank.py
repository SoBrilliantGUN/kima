from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class RerankResult:
    """单条文档的精排结果：index 对应入参 documents 的下标，score 为相关度（0~1）。"""

    index: int
    score: float


class RerankerClient(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[RerankResult]: ...


class FakeRerankerClient:
    """确定性伪 rerank：按原顺序给递减分数（index 越小分越高），供测试链路。"""

    async def rerank(self, query: str, documents: list[str]) -> list[RerankResult]:
        n = len(documents)
        return [RerankResult(index=i, score=(n - i) / n) for i in range(n)]


class SiliconFlowRerankerClient:
    """SiliconFlow `/rerank` 实现（model=bge-reranker-v2-m3，Jina 风格响应）。

    返回按 relevance_score 降序的 RerankResult 列表，调用方自行取 top-N。
    """

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    async def rerank(self, query: str, documents: list[str]) -> list[RerankResult]:
        if not documents:
            return []
        payload: dict[str, object] = {
            "model": self._model,
            "query": query,
            "documents": documents,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/rerank", json=payload, headers=headers
            )
        response.raise_for_status()

        data: Any = response.json()
        results = data.get("results", [])
        return [
            RerankResult(index=int(item["index"]), score=float(item["relevance_score"]))
            for item in results
        ]
