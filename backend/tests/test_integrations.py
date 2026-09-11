from app.core.config import Settings
from app.integrations import get_document_parser, get_embedding_client, get_llm_client
from app.integrations.llm import ChatMessage
from app.integrations.parser import SourceType


async def test_fake_llm_echoes_last_user_message() -> None:
    client = get_llm_client(Settings(llm_provider="fake"))
    result = await client.chat(
        [
            ChatMessage(role="user", content="第一句"),
            ChatMessage(role="assistant", content="忽略我"),
            ChatMessage(role="user", content="最后一句"),
        ]
    )
    assert result.content == "[fake] 最后一句"


async def test_fake_embedding_dimension_and_determinism() -> None:
    client = get_embedding_client(Settings(embedding_provider="fake", embedding_dim=1024))
    assert client.dimension == 1024

    vectors = await client.embed_documents(["你好", "世界"])
    assert len(vectors) == 2
    assert all(len(vector) == 1024 for vector in vectors)

    # 确定性：同一输入、重复调用结果一致
    assert await client.embed_query("你好") == vectors[0]


async def test_fake_parser_returns_markdown() -> None:
    parser = get_document_parser(Settings())
    document = await parser.parse(source_type=SourceType.PDF)
    assert document.markdown.startswith("#")
    assert document.metadata["source_type"] == "pdf"
