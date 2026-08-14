from datetime import UTC, datetime

from auspex.collectors.news_collector import NewsCollector
from auspex.models.common import utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType
from auspex.persistence.memory import InMemoryDocumentSink, InMemoryWatermarkStore
from auspex.providers.base import NewsArticleDTO


class Provider:
    async def get_news(self, ticker, since):
        return [
            NewsArticleDTO(
                ticker=ticker,
                external_id="article-1",
                title="Intel launches a new product",
                url="https://example.com/intel",
                published_at=datetime(2026, 8, 13, tzinfo=UTC),
                content_hash="sha256:same",
                body_text="Company-specific provider summary.",
            )
        ]


async def test_duplicate_news_is_enriched_with_provider_summary() -> None:
    sink = InMemoryDocumentSink()
    await sink.upsert_document(
        Document(
            id="document-1",
            security_id="sec-intc",
            source="finnhub",
            source_record_id="article-1",
            document_type=DocumentType.NEWS,
            title="Intel launches a new product",
            url="https://example.com/intel",
            published_at=datetime(2026, 8, 13, tzinfo=UTC),
            content_hash="sha256:same",
            retrieved_at=utc_now(),
            knowledge_date=datetime(2026, 8, 13, tzinfo=UTC).date(),
        )
    )
    collector = NewsCollector(Provider(), sink, InMemoryWatermarkStore())

    result = await collector.collect(
        "sec-intc",
        "INTC",
        datetime(2026, 8, 12, tzinfo=UTC),
    )

    enriched = await sink.find_by_content_hash("sec-intc", "sha256:same")
    assert enriched is not None
    assert enriched.content_excerpt == "Company-specific provider summary."
    assert result.items_written == 1
    assert result.new_document_ids == ["document-1"]
