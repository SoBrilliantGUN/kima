"""Word 本地解析（mammoth）。

``.docx`` → markdown。mammoth 的 markdown 输出不支持表格，故先转 HTML 再经 markdownify
转回 markdown，把 ``<table>`` 保留成 GFM 表格。CPU 同步转换用 to_thread 包一层。
"""

import asyncio
import io

import mammoth
from markdownify import markdownify as html_to_markdown

from app.integrations.parser import ParsedDocument


class WordDocumentParser:
    """``.docx`` → markdown 的窄解析器（mammoth + markdownify 兜底转 GFM 表格）。"""

    async def parse(self, *, content: bytes) -> ParsedDocument:
        def _convert() -> str:
            with io.BytesIO(content) as buffer:
                html = mammoth.convert_to_html(buffer).value
            return html_to_markdown(html)

        markdown = await asyncio.to_thread(_convert)
        return ParsedDocument(
            markdown=markdown,
            metadata={
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
        )
