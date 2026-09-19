from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class WebSearchResult:
    """全网搜索结果：标题 / 链接 / 摘要片段。"""

    title: str
    url: str
    snippet: str


class WebSearchClient(Protocol):
    async def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]: ...


class FakeWebSearchClient:
    """确定性伪结果，供测试链路（默认 provider）。"""

    async def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
        return [
            WebSearchResult(
                title=f"[fake] {query} · 结果 {i}",
                url=f"https://example.com/{i}",
                snippet=f"这是第 {i} 条伪搜索结果片段。",
            )
            for i in range(top_k)
        ]


class BochaWebSearchClient:
    """博查 Web Search API 实现（Bing 兼容响应格式）。

    响应 `data.webPages.value[]`，字段 `name`/`url`/`snippet`（`summary` 作为回退）。
    """

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
        payload: dict[str, object] = {"query": query, "count": top_k}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/web-search", json=payload, headers=headers
            )
        response.raise_for_status()

        data: Any = response.json()
        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        return [
            WebSearchResult(
                title=str(page.get("name") or page.get("title") or ""),
                url=str(page.get("url") or ""),
                snippet=str(page.get("snippet") or page.get("summary") or ""),
            )
            for page in pages
        ]
