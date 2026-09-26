import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import json_repair
from pydantic import BaseModel, ValidationError

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

    # 流式：调用即返回 AsyncIterator（async generator），`async for` 逐 token 消费
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...


class FakeLLMClient:
    """回显最后一条 user 内容；stream 按小块 yield 模拟流式。"""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        return ChatResult(content=f"[fake] {self._last_user(messages)}")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        text = f"[fake] {self._last_user(messages)}"
        for i in range(0, len(text), 8):
            yield text[i : i + 8]

    @staticmethod
    def _last_user(messages: list[ChatMessage]) -> str:
        return next((m.content for m in reversed(messages) if m.role == "user"), "")


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

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions", json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)


class StructuredParseError(Exception):
    """LLM 结构化输出在语法/结构/语义层校验失败（携带可回喂模型的精确错误）。

    对应「结构化输出」文章的双层防线：``json.loads`` 只守住语法层，本异常用于把
    「字段名漂移 / 类型错 / 枚举越界 / 数组长度不符」等结构·语义层失败显式抛给
    自纠错循环，而不是静默降级成「默认值」——后者正是「格式完美的幻觉值污染下游」的入口。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_CODE_FENCE_RE = re.compile(r"^```[^\r\n]*\r?\n(.*?)\r?\n```$", re.DOTALL)


def strip_code_fence(raw: str) -> str:
    """剥掉 LLM 常见的 ```json 代码块外壳与首尾空白。

    用正则锚定 fence 结构（开头的 ``` + 语言标签 + 换行，结尾的 ```），只剥外壳，
    不动内容里出现的 ``json`` 字面量与反引号——此前 ``replace("json", "", 1)`` 会在
    fence 无语言标签或标签非小写 ``json`` 时误删内容，产生静默损坏。
    """
    text = raw.strip()
    match = _CODE_FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def first_validation_error(exc: ValidationError) -> str:
    """取 Pydantic 校验链的第一条错误，格式化为「字段 X 校验失败：规则」摘要。

    只回喂最关键的字段级错误，而不是整串 ValidationError（文章「错误摘要要精确」）。
    """
    err = exc.errors()[0]
    loc = ".".join(str(part) for part in err["loc"])
    return f"字段 {loc} 校验失败：{err['msg']}"


def loads_json_repair(raw: str) -> Any:
    """解析 LLM 输出的 JSON：剥代码块外壳 → ``json.loads`` → 失败则 ``json_repair`` 修复。

    替代「喂回 LLM 再试一轮」的自纠错（决策 D5）：语法层坏 JSON（缺逗号/尾逗号/未加引号
    key 等）用确定性 ``json_repair`` 修，不再多烧一次 LLM 调用；语义层（枚举越界/字段类型
    错）仍由调用方的严格校验兜底。两者都失败抛 ``StructuredParseError``（fail-closed）。
    """
    text = strip_code_fence(raw)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return json_repair.loads(text)
    except Exception as exc:  # noqa: BLE001 - json_repair 可抛多种解析异常，统一 fail-closed
        raise StructuredParseError(f"输出不是合法 JSON：{exc}") from exc


def format_instructions[T: BaseModel](model: type[T]) -> str:
    """把 pydantic 模型序列化成「完整 JSON Schema」格式说明，供拼进提示词。

    替代在各调用点手写 JSON 示例：字段语义写进 ``Field(description=...)``，
    ``model_json_schema()`` 直接吐出带语义的完整 schema（含嵌套/枚举），一份顶全部。
    对象与数组（RootModel）通用。
    """
    schema = json.dumps(model.model_json_schema(), ensure_ascii=False)
    return (
        "只输出一个符合以下 JSON Schema 的 JSON，"
        "不要输出其它任何文字、解释或 markdown 代码块：\n"
        f"{schema}"
    )


def parse_json[T: BaseModel](raw: str, model: type[T]) -> T:
    """语法层修复 + 结构/语义层严格校验。

    ``loads_json_repair`` 修坏 JSON（缺逗号/尾逗号/未加引号 key/代码块外壳），
    ``model_validate`` 强制字段名/类型/枚举合法；任一失败抛 ``StructuredParseError``
    （fail-closed），不回退默认值——那正是「格式完美的幻觉值污染下游」的入口。
    """
    data = loads_json_repair(raw)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise StructuredParseError(first_validation_error(exc)) from exc
