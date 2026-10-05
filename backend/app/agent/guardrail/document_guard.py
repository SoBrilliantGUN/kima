"""KB 写库闸（trust chain）：文档入库前用零信任评分拦截投毒。

查询时只能隔离已入库的毒，治标；真正的预防是把投毒文档挡在入库之前——审查一次
等于出口审查一万次。写库闸在 ingest 解析后、分块前扫全文，分两层：

- **红线**（硬注入正则）命中 → 不再一票否决拒绝，而是把命中片段（含前后文）提取出来，
  交给用户确认：确认后作为低信任文档入库（``injection_approved``），检索侧以 ``<data>``
  隔离而非硬阻断（见 `find_red_line_violations` / ingest 的 needs_approval 分支）。
- **综合分落到阻断档**（来源/内容双低，软信号）→ 仍拒绝（不重试）。
- 其余（密度软信号仅拉低分数）→ 放行，``<data>`` 隔离留到检索侧按分处置。
"""

from app.agent.guardrail.injection import RedLineHit, find_red_line_hits
from app.agent.guardrail.trust import (
    Disposition,
    composite,
    content_trust,
    disposition,
    source_trust,
)
from app.core.exceptions import DomainError


class PoisonedDocumentError(DomainError):
    """文档包含提示注入内容，拒绝入库（不重试）。"""

    code = "poisoned_document"


def find_red_line_violations(text: str) -> list[RedLineHit]:
    """扫描全文的注入红线命中（含前后 100 字上下文），供待确认态高亮展示。

    只定位不裁决：是否放行由用户确认（``injection_approved``）决定，这里不做否决。
    """
    return find_red_line_hits(text)


def guard_document_text(text: str) -> None:
    """软信号写库闸：综合分落到阻断档即抛 PoisonedDocumentError（红线已上移由调用方处置）。"""
    score = composite(content_trust(text), source=source_trust("document"))
    if disposition(score) is Disposition.BLOCK:
        raise PoisonedDocumentError(f"文档可信度过低（综合分 {score:.0f}），拒绝入库")
