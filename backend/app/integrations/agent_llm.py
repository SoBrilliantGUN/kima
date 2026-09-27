"""Agent 层 LLM（LangChain ChatModel）。

与模块 5 的 `LLMClient`（自建 httpx 协议）不同，Agent 编排走 LangChain 生态：
`create_agent` 需要 `BaseChatModel`（支持 `bind_tools`）。`fake` provider 返回脚本化
假模型供本地开发与测试（测试再注入自己的脚本化响应覆盖回环）。

复用 `settings.llm_model` / `llm_api_key` / `llm_base_url`，不新增模型档位。
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from app.core.config import Settings

AGENT_TEMPERATURE = 0.3

# fake provider 的兜底回复（真实运行时请配 deepseek）；仅本地开发占位。
_FAKE_RESPONSE = "[fake] Copilot：请配置真实 DeepSeek（LLM_PROVIDER=deepseek）后使用。"


class FakeAgentModel(FakeMessagesListChatModel):
    """脚本化假模型：忽略 `bind_tools`（不校验工具 schema），按 responses 顺序回放。

    `create_agent` 会调用 `bind_tools` 注入工具定义，`FakeMessagesListChatModel` 原生
    不支持（抛 NotImplementedError）；此处返回 self 使编排层在 fake 下也能跑通工具回环。
    """

    def bind_tools(self, tools: object, **kwargs: object) -> "FakeAgentModel":
        del tools, kwargs  # 忽略工具定义，脚本化回放无需校验
        return self


def get_agent_model(settings: Settings) -> BaseChatModel:
    """按 `settings.llm_provider` 返回 Agent 用的 LangChain ChatModel。"""
    if settings.llm_provider == "deepseek":
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "temperature": AGENT_TEMPERATURE,
        }
        # api_key 为空时不传：显式传 "" 会覆盖 DEEPSEEK_API_KEY 环境变量，
        # 且构造时抛 openai.OpenAIError: Missing credentials；留空让 ChatDeepSeek 读环境变量兜底。
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        # ChatDeepSeek 的规范字段是 api_base（base_url 仅 langchain-deepseek>=1.1.0 起的别名），
        # 默认 https://api.deepseek.com/v1；此处复用 llm_base_url 以支持自定义网关/代理。
        if settings.llm_base_url:
            kwargs["api_base"] = settings.llm_base_url
        return ChatDeepSeek(**kwargs)
    if settings.llm_provider == "fake":
        return FakeAgentModel(responses=[AIMessage(content=_FAKE_RESPONSE)])
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
