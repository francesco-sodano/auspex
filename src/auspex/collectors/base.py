"""Collector shared contracts (arc42 §5.3).

Deduplication key: ``source + source_record_id + content_hash``. Identical
content hash -> skipped entirely, no storage write, no LLM call. An
amendment has a new accession and supersedes its predecessor via
``supersedes_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from auspex.models.document import Document
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.market import FxRate, PriceBar


class WatermarkStore(Protocol):
    async def get_watermark(self, key: str) -> str | None: ...
    async def set_watermark(self, key: str, value: str) -> None: ...


class DocumentSink(Protocol):
    async def upsert_document(self, doc: Document) -> None: ...
    async def find_by_content_hash(self, security_id: str, content_hash: str) -> Document | None: ...


class PriceSink(Protocol):
    async def upsert_price_bar(self, bar: PriceBar) -> None: ...


class FxSink(Protocol):
    async def upsert_fx_rate(self, rate: FxRate) -> None: ...


class FundamentalSink(Protocol):
    async def upsert_fundamental_snapshot(self, snapshot: FundamentalSnapshot) -> None: ...


class BlobSink(Protocol):
    async def upload_document_blob(self, security_id: str, document_id: str, ext: str, content: bytes | str) -> str: ...
    async def upload_section_blob(self, security_id: str, document_id: str, item: str, content: str) -> str: ...


@dataclass
class CollectorResult:
    collector: str
    security_id: str | None
    items_seen: int = 0
    items_written: int = 0
    items_skipped_duplicate: int = 0
    degraded: bool = False
    error: str | None = None
    new_document_ids: list[str] = field(default_factory=list)


def watermark_key(collector: str, security_id: str) -> str:
    return f"{collector}:{security_id}"
