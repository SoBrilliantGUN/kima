"""查询改写：把含指代的问题改写成独立检索 query（喂最近 1~2 轮历史）。"""

from app.integrations.llm import ChatMessage, LLMClient

_REWRITE_SYSTEM = (
    "你是查询改写助手。把用户问题改写成不依赖对话上下文的独立检索查询，"
    "用于在知识库中检索。保留原意、消解指代（如「它」「那个」）。"
    "只输出改写后的查询，不要解释。"
)


async def rewrite_query(llm: LLMClient, query: str, history: list[ChatMessage]) -> str:
    """改写查询；`history` 为最近的若干轮对话（用于消解指代）。"""
    messages = [ChatMessage("system", _REWRITE_SYSTEM), *history, ChatMessage("user", query)]
    result = await llm.chat(messages, temperature=0)
    return result.content.strip()
