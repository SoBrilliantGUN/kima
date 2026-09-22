"""KB 写库闸（trust chain）：文档入库前用零信任评分拦截投毒。

查询时只能隔离已入库的毒，治标；真正的预防是把投毒文档挡在入库之前——审查一次
等于出口审查一万次。`guard_document_text` 在 ingest 解析后、分块前扫全文：
- 命中**红线**（硬注入正则）→ 直接拒绝入库（不重试）。
- 综合分落到**阻断**档（来源/内容双低）→ 拒绝。
- 其余（密度软信号仅拉低分数）→ 放行，<data> 隔离留到检索侧按分处置。
"""

from app.agent.guardrail.trust import (
    Disposition,
    composite,
    content_trust,
    disposition,
    is_red_line,
    source_trust,
)
from app.core.exceptions import DomainError


class PoisonedDocumentError(DomainError):
    """文档包含提示注入内容，拒绝入库（不重试）。"""

    code = "poisoned_document"


def guard_document_text(text: str) -> None:
    """扫描待入库文档全文；命中红线 / 综合分过低即抛 PoisonedDocumentError。"""
    if is_red_line(text):
        raise PoisonedDocumentError("文档包含提示注入内容（命中红线），拒绝入库")
    score = composite(content_trust(text), source=source_trust("document"))
    if disposition(score) is Disposition.BLOCK:
        raise PoisonedDocumentError(f"文档可信度过低（综合分 {score:.0f}），拒绝入库")
