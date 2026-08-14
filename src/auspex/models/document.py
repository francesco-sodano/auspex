"""Ingested document envelope (`documents` container, arc42 §5.3, §5.11).

Covers SEC filings (10-K/10-Q/8-K/20-F/6-K/S-1), Form 4 insider transactions,
and news articles. Deduplication key is ``source + source_record_id +
content_hash``; an amendment gets a new accession and supersedes its
predecessor via ``supersedes_id``.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import DocumentType, Form4TransactionCode


class InsiderTransaction(AuspexModel):
    """A single non-derivative transaction row extracted from a Form 4 XML."""

    owner_name: str
    is_officer: bool = False
    is_director: bool = False
    is_ten_percent_owner: bool = False
    transaction_code: Form4TransactionCode
    transaction_date: date
    shares: str  # Decimal-as-string
    price_per_share: str  # Decimal-as-string


class Document(AuspexModel):
    id: str = Field(description="document_id")
    security_id: str
    source: str = Field(description="edgar | tiingo | finnhub | manual")
    source_record_id: str = Field(description="accession number or provider article id")
    document_type: DocumentType
    form_type: str | None = None
    accession_number: str | None = None
    filed_date: date | None = Field(default=None, description="EDGAR `filed` — point-in-time cutoff")
    period_end_date: date | None = None
    published_at: datetime | None = Field(default=None, description="news publication timestamp")
    title: str | None = None
    url: str | None = None
    content_excerpt: str | None = Field(
        default=None,
        description="Provider-supplied news summary/body excerpt, capped at ingestion.",
    )
    content_hash: str
    supersedes_id: str | None = None
    blob_path: str | None = Field(default=None, description="documents/{security_id}/{document_id}.{ext}")
    section_blob_paths: dict[str, str] = Field(
        default_factory=dict, description="item -> sections/{security_id}/{document_id}/{item}.txt"
    )
    insider_transactions: list[InsiderTransaction] = Field(default_factory=list)
    retrieved_at: datetime
    knowledge_date: date = Field(
        description="date this document becomes visible to point-in-time scoring: "
        "filed_date for filings/Form 4, published_at.date() for news"
    )

    @property
    def partition_key(self) -> str:
        return self.security_id
