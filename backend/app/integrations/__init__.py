from functools import lru_cache

from app.core.config import Settings
from app.integrations.embedding import EmbeddingClient, FakeEmbeddingClient
from app.integrations.llm import FakeLLMClient, LLMClient
from app.integrations.parser import DocumentParser, FakeDocumentParser

__all__ = [
    "LLMClient",
    "EmbeddingClient",
    "DocumentParser",
    "get_llm_client",
    "get_embedding_client",
    "get_document_parser",
]


def get_llm_client(settings: Settings) -> LLMClient:
    return _llm_client(settings.llm_provider, settings.llm_model)


@lru_cache(maxsize=1)
def _llm_client(provider: str, model: str) -> LLMClient:
    if provider == "fake":
        return FakeLLMClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def get_embedding_client(settings: Settings) -> EmbeddingClient:
    return _embedding_client(
        settings.embedding_provider, settings.embedding_model, settings.embedding_dim
    )


@lru_cache(maxsize=1)
def _embedding_client(provider: str, model: str, dimension: int) -> EmbeddingClient:
    if provider == "fake":
        return FakeEmbeddingClient(dimension=dimension)
    raise ValueError(f"Unsupported Embedding provider: {provider}")


def get_document_parser(settings: Settings) -> DocumentParser:
    # 模块 1 仅 fake；模块 4 接入 MinerU 时按 mineru_api_base_url 注册真实实现
    return _document_parser(settings.mineru_api_base_url)


@lru_cache(maxsize=1)
def _document_parser(base_url: str) -> DocumentParser:
    return FakeDocumentParser()
