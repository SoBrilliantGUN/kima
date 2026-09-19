"""RagService 门面：`retrieve()`（检索）与 `answer()`（生成），编排层可替换。

`answer()` = 改写 → 检索（KB / 全网）→ 组装上下文 + 历史（token 预算 + 摘要）→ 流式生成。
将来上 LangGraph Agentic 时只在 `answer()` 编排层替换，检索层与业务层不动。
"""

import uuid
from collections.abc import AsyncIterator

from app.chunking.base import estimate_tokens
from app.integrations.llm import ChatMessage, LLMClient
from app.integrations.search import WebSearchClient
from app.rag.context import (
    assemble_history,
    format_kb_context,
    format_web_context,
)
from app.rag.generate import AnswerCitations, AnswerDelta, AnswerEvent
from app.rag.retriever import RagRetriever
from app.rag.rewrite import rewrite_query
from app.rag.schema import RetrievedChunk

GENERATE_TEMPERATURE = 0.3
SAFETY_MARGIN_TOKENS = 200
WEB_TOP_K = 5

_GENERATE_SYSTEM = (
    "你是 kima 知识库问答助手。根据提供的参考资料回答用户问题。"
    "回答时在引用处标注来源编号（如 [1][2]），编号对应参考资料条目。"
    "如果参考资料不足以回答，诚实说明，不要编造。"
)


class RagService:
    """RAG 门面：检索与生成编排，五步各为独立可替换组件。"""

    def __init__(
        self,
        *,
        retriever: RagRetriever,
        llm: LLMClient,
        web_search: WebSearchClient,
        context_max_tokens: int,
        history_recent_turns: int,
    ) -> None:
        self._retriever = retriever
        self._llm = llm
        self._web_search = web_search
        self._context_max_tokens = context_max_tokens
        self._history_recent_turns = history_recent_turns

    async def retrieve(self, query: str, kb_ids: list[uuid.UUID]) -> list[RetrievedChunk]:
        """检索指定知识库集合（混合检索 + RRF + rerank + 回 parent）。"""
        return await self._retriever.retrieve(query, kb_ids)

    async def answer(
        self,
        *,
        query: str,
        kb_ids: list[uuid.UUID],
        history: list[ChatMessage],
    ) -> AsyncIterator[AnswerEvent]:
        """完整 RAG 生成：先 yield 文本增量，最后 yield 引用。`kb_ids` 空 = 联网搜索。"""
        # ① 改写（喂最近 2 轮历史消解指代）
        rewritten = await rewrite_query(self._llm, query, history[-4:])

        # ②③④ 检索（KB 混合检索 或 全网）
        if not kb_ids:
            results = await self._web_search.search(rewritten, top_k=WEB_TOP_K)
            context, citations = format_web_context(results)
        else:
            chunks = await self._retriever.retrieve(rewritten, kb_ids)
            context, citations = format_kb_context(chunks)

        # 历史 token 预算：总窗口扣掉 system/context/question 后留给历史
        fixed_tokens = (
            estimate_tokens(_GENERATE_SYSTEM)
            + estimate_tokens(context)
            + estimate_tokens(query)
            + SAFETY_MARGIN_TOKENS
        )
        history_budget = max(0, self._context_max_tokens - fixed_tokens)
        history_messages = await assemble_history(
            self._llm,
            history,
            recent_turns=self._history_recent_turns,
            history_budget=history_budget,
        )

        # ⑤ 组装最终 messages
        messages = [ChatMessage("system", _GENERATE_SYSTEM), *history_messages]
        if context:
            messages.append(ChatMessage("user", f"参考资料：\n{context}"))
        messages.append(ChatMessage("user", query))

        # 流式生成
        async for delta in self._llm.stream(messages, temperature=GENERATE_TEMPERATURE):
            yield AnswerDelta(delta)

        # 引用在生成完成后补齐
        yield AnswerCitations(citations)
