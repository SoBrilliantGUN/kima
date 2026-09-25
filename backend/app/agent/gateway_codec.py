"""LLM 网关的序列化 / 指纹 / 脱敏纯函数（无状态，供 ``LLMGateway`` 调用）。

三类职责：
- 用量提取与序列化（Usage ↔ dict、结果 ↔ JSON dict）
- 内容指纹（call_key 的规范化输入哈希）
- 出站 DLP 脱敏（软改写副本，不改快照指纹）
"""

import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.agent.guardrail.sensitive import redact_sensitive
from app.agent.runtime.budget import Usage, extract_usage
from app.integrations.llm import ChatMessage, ChatResult

__all__ = [
    "aimessage_to_dict",
    "canonical_chat",
    "canonical_langchain",
    "chat_result_to_dict",
    "dict_to_aimessage",
    "dict_to_chat_result",
    "estimate_text_tokens",
    "make_call_key",
    "redact_chat",
    "redact_langchain",
    "usage_from_aimessage",
    "usage_from_chat_result",
    "usage_to_dict",
]


def usage_from_chat_result(result: ChatResult) -> Usage:
    """从 LLMClient 的 ChatResult 提取用量（LLMClient 不报 cache，cache_read 恒 0）。"""
    return Usage(
        input_tokens=result.prompt_tokens or 0,
        output_tokens=result.completion_tokens or 0,
    )


def usage_from_aimessage(message: AIMessage) -> Usage:
    """从 LangChain AIMessage 提取用量（含 cache 明细，兼容 DeepSeek 顶层缓存字段）。

    ``usage_metadata`` 承载归一化的 input/output/cache_read；DeepSeek 的缓存命中
    （``prompt_cache_hit_tokens``）不经归一化，落在原始 ``response_metadata.token_usage``，
    作为 cache_read 的兜底来源传入 ``extract_usage``。
    """
    raw_usage = (getattr(message, "response_metadata", None) or {}).get("token_usage")
    return extract_usage(getattr(message, "usage_metadata", None), raw_usage)


def usage_to_dict(usage: Usage) -> dict[str, Any]:
    """把用量序列化为快照落库的 JSON 字段（供 ``Snapshots.put`` 存储三轴用量）。"""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def chat_result_to_dict(result: ChatResult) -> dict[str, Any]:
    """形态 A 结果编码：把 ``ChatResult`` 序列化为快照可存的 JSON dict。

    与 ``dict_to_chat_result`` 成对，作为网关 ``_invoke`` 的 encode/decode 回调。
    """
    return {
        "content": result.content,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


def dict_to_chat_result(data: dict[str, Any]) -> ChatResult:
    """形态 A 结果解码：快照命中时把 JSON dict 还原为 ``ChatResult``。

    content 强转 str 兜底；model/tokens 允许缺省（旧快照可能未落这些字段）。
    """
    return ChatResult(
        content=str(data["content"]),
        model=data.get("model"),
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
    )


def aimessage_to_dict(message: AIMessage) -> dict[str, Any]:
    """形态 B 结果编码：把 ``AIMessage`` 序列化为快照可存的 JSON dict。

    只保留 content / tool_calls / id 三个可重放字段，与 ``dict_to_aimessage`` 成对。
    """
    return {"content": message.content, "tool_calls": message.tool_calls or [], "id": message.id}


def dict_to_aimessage(data: dict[str, Any]) -> AIMessage:
    """形态 B 结果解码：快照命中时把 JSON dict 还原为 ``AIMessage``（tool_calls 缺省空表）。"""
    return AIMessage(
        content=data.get("content", ""),
        tool_calls=data.get("tool_calls") or [],
        id=data.get("id"),
    )


def make_call_key(node: str, fingerprint: str) -> str:
    """由 node 与指纹拼出快照缓存键（call_key）。

    指纹为 ``canonical_*`` 的规范化序列化，二者以 ``\\x00`` 分隔，避免 ("a","bc") 与
    ("ab","c") 的拼接歧义；取 sha256 前 32 位十六进制（128-bit）作短键，同时用作快照
    复用与成本审计的落库键（``gateway.py:_invoke``）。
    """
    return hashlib.sha256(f"{node}\x00{fingerprint}".encode()).hexdigest()[:32]


def estimate_text_tokens(texts: list[str]) -> int:
    """嵌入/精排接口不返回 token 用量，按字符数粗估（~4 字符/token，保底 1）。"""
    return sum(max(1, len(t) // 4) for t in texts)


def canonical_chat(messages: list[ChatMessage]) -> str:
    """形态 A 指纹：把 ``ChatMessage`` 列表规范化为稳定 JSON 串（供 ``make_call_key`` 哈希）。

    固定 role/content 两字段，``sort_keys=True`` 保证键序稳定、``ensure_ascii=False``
    保留原文不转义。在 DLP 脱敏之前计算，故快照键不随脱敏副本漂移。
    """
    return json.dumps(
        [{"role": m.role, "content": m.content} for m in messages],
        ensure_ascii=False,
        sort_keys=True,
    )


def redact_chat(messages: list[ChatMessage]) -> list[ChatMessage]:
    """出站 DLP（决策 #23 防线③）：把发给 LLM 的消息内容脱敏后再发（软改写，不留原始敏感值）。

    只改「发出去」的副本，不改快照指纹（``canonical_chat`` 仍在脱敏前计算），故快照
    缓存键保持语义稳定，重放时仍能命中。软改写不否决调用——敏感值打码后照常发，方向与
    注入闸「命中即拒」不同（见 ``guardrail/sensitive.py``）。
    """
    return [ChatMessage(m.role, redact_sensitive(m.content)) for m in messages]


def redact_langchain(messages: list[BaseMessage]) -> list[BaseMessage]:
    """同 ``redact_chat``，但作用于 LangChain BaseMessage（agent 主循环/planner/qa）。"""
    out: list[BaseMessage] = []
    for m in messages:
        content = m.content
        if isinstance(content, str) and content:
            content = redact_sensitive(content)
        out.append(m.model_copy(update={"content": content}))
    return out


def canonical_langchain(messages: list[BaseMessage]) -> str:
    """形态 B 指纹：把 ``BaseMessage`` 列表规范化为稳定 JSON 串（供 ``make_call_key`` 哈希）。

    每条消息固定 type/content；AIMessage 附带 tool_calls（仅 id/name/args），ToolMessage
    附带 tool_call_id，忽略易变的元数据字段，保证同语义消息哈希一致。``default=str``
    兜底不可 JSON 序列化的内容。
    """
    parts: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"type": m.type, "content": m.content}
        if isinstance(m, AIMessage):
            item["tool_calls"] = [
                {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
                for tc in (m.tool_calls or [])
            ]
        elif isinstance(m, ToolMessage):
            item["tool_call_id"] = m.tool_call_id
        parts.append(item)
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
