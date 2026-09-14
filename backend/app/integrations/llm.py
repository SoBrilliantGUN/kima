from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMClient(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult: ...


class FakeLLMClient:
    """回显最后一条 user 内容，供模块 1 打通链路。"""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"), ""
        )
        return ChatResult(content=f"[fake] {last_user}")


class DeepSeekLLMClient:
    """DeepSeek（OpenAI 兼容）实现，httpx 异步调用 /chat/completions。"""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
        response.raise_for_status()

        data: Any = response.json()
        choices = data["choices"]
        message = choices[0]["message"]
        usage = data.get("usage") or {}
        model = data.get("model")
        return ChatResult(
            content=str(message["content"]),
            model=str(model) if model is not None else None,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
