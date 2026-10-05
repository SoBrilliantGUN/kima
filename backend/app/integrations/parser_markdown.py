"""Markdown(.md) 本地解析：bytes → markdown 字符串。

MD 文件内容本身就是 markdown，无需第三方转换；只做编码解码（UTF-8 优先，回退 GBK，
最后兜底 replace，避免乱字节抛异常）。
"""

from app.integrations.parser import ParsedDocument


class MarkdownDocumentParser:
    """``.md`` → markdown 的窄解析器：按编码解码字节为文本。"""

    async def parse(self, *, content: bytes) -> ParsedDocument:
        return ParsedDocument(
            markdown=self._decode(content),
            metadata={"mime_type": "text/markdown"},
        )

    @staticmethod
    def _decode(content: bytes) -> str:
        for encoding in ("utf-8", "gbk"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
