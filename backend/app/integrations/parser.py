from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class SourceType(StrEnum):
    PDF = "pdf"
    URL = "url"
    WORD = "word"
    TEXT = "text"  # txt / markdown 等纯文本


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class DocumentParser(Protocol):
    async def parse(
        self,
        *,
        source_type: SourceType,
        file_path: str | None = None,
        content: bytes | None = None,
        url: str | None = None,
    ) -> ParsedDocument: ...


class FakeDocumentParser:
    async def parse(
        self,
        *,
        source_type: SourceType,
        file_path: str | None = None,
        content: bytes | None = None,
        url: str | None = None,
    ) -> ParsedDocument:
        return ParsedDocument(
            markdown=f"# [fake] parsed {source_type.value}",
            title=f"fake-{source_type.value}",
            metadata={"source_type": source_type.value},
        )
