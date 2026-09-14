"""LLM 相关操作集中管理：提示词、调用参数与降级策略。

业务层（如 NoteService）不直接操作 LLM 客户端，而是依赖本模块提供的
操作类；后续新增 LLM 能力（问答、打标、改写等）也统一收在这里，便于
集中维护提示词与模型参数。
"""

from app.integrations.llm import ChatMessage, LLMClient

# 网页摘要：系统提示词与正文截断长度（避免超长内容撑爆上下文）。
SUMMARY_SYSTEM_PROMPT = "你是摘要助手，用一到两句中文概括网页正文"
SUMMARY_MAX_CHARS = 4000
SUMMARY_FALLBACK_CHARS = 200


class Summarizer:
    """网页摘要生成器：调用 LLM 生成一到两句摘要，失败时降级为截断正文。"""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def summarize(self, markdown: str) -> str:
        messages = [
            ChatMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
            ChatMessage(role="user", content=markdown[:SUMMARY_MAX_CHARS]),
        ]
        try:
            result = await self._llm.chat(messages, temperature=0.3, max_tokens=150)
            return result.content
        except Exception:
            truncated = markdown[:SUMMARY_FALLBACK_CHARS]
            return truncated + ("…" if len(markdown) > SUMMARY_FALLBACK_CHARS else "")
