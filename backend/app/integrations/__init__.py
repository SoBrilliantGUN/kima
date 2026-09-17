from functools import lru_cache

from app.core.config import Settings
from app.integrations.embedding import (
    EmbeddingClient,
    FakeEmbeddingClient,
    SiliconFlowEmbeddingClient,
)
from app.integrations.llm import DeepSeekLLMClient, FakeLLMClient, LLMClient
from app.integrations.parser import (
    DispatchDocumentParser,
    DocumentParser,
    MinerUDocumentParser,
    WebDocumentParser,
    WordDocumentParser,
)

__all__ = [
    "LLMClient",
    "EmbeddingClient",
    "DocumentParser",
    "get_llm_client",
    "get_embedding_client",
    "get_document_parser",
]


def get_llm_client(settings: Settings) -> LLMClient:
    return _llm_client(
        settings.llm_provider, settings.llm_model, settings.llm_base_url, settings.llm_api_key
    )


@lru_cache(maxsize=1)
def _llm_client(provider: str, model: str, base_url: str, api_key: str) -> LLMClient:
    if provider == "fake":
        return FakeLLMClient()
    if provider == "deepseek":
        return DeepSeekLLMClient(base_url=base_url, api_key=api_key, model=model)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def get_embedding_client(settings: Settings) -> EmbeddingClient:
    return _embedding_client(
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_base_url,
        settings.embedding_api_key,
    )


@lru_cache(maxsize=1)
def _embedding_client(
    provider: str, model: str, dimension: int, base_url: str, api_key: str
) -> EmbeddingClient:
    if provider == "fake":
        return FakeEmbeddingClient(dimension=dimension)
    if provider == "siliconflow":
        return SiliconFlowEmbeddingClient(
            base_url=base_url, api_key=api_key, model=model, dimension=dimension
        )
    raise ValueError(f"Unsupported Embedding provider: {provider}")


def get_document_parser(settings: Settings) -> DocumentParser:
    return _document_parser(settings.mineru_api_base_url, settings.mineru_api_token)


@lru_cache(maxsize=1)
def _document_parser(mineru_base_url: str, mineru_token: str) -> DocumentParser:
    return DispatchDocumentParser(
        pdf=MinerUDocumentParser(base_url=mineru_base_url, token=mineru_token),
        word=WordDocumentParser(),
        web=WebDocumentParser(),
    )
