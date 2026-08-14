"""`NewsCollector` — per-security news articles (arc42 §5.3)."""

from __future__ import annotations

from datetime import datetime

from auspex.collectors.base import CollectorResult, DocumentSink, WatermarkStore, watermark_key
from auspex.models.common import new_id, utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType
from auspex.providers.base import NewsProvider

COLLECTOR_NAME = "news"


class NewsCollector:
    def __init__(self, provider: NewsProvider, document_sink: DocumentSink, watermarks: WatermarkStore) -> None:
        self._provider = provider
        self._document_sink = document_sink
        self._watermarks = watermarks

    async def collect(self, security_id: str, ticker: str, default_since: datetime) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, security_id)
        watermark = await self._watermarks.get_watermark(key)
        since = datetime.fromisoformat(watermark) if watermark else default_since

        result = CollectorResult(collector=COLLECTOR_NAME, security_id=security_id)
        try:
            articles = await self._provider.get_news(ticker, since)
        except Exception as exc:  # noqa: BLE001
            result.degraded = True
            result.error = str(exc)
            return result

        result.items_seen = len(articles)
        latest_published = None
        for article in articles:
            existing = await self._document_sink.find_by_content_hash(security_id, article.content_hash)
            if existing is not None:
                result.items_skipped_duplicate += 1
                if not existing.content_excerpt and article.body_text:
                    enriched = existing.model_copy(
                        update={
                            "content_excerpt": article.body_text[:4000],
                            "title": existing.title or article.title,
                            "url": existing.url or article.url,
                            "retrieved_at": utc_now(),
                        }
                    )
                    await self._document_sink.upsert_document(enriched)
                    result.items_written += 1
                    result.new_document_ids.append(existing.id)
            else:
                document_id = new_id()
                doc = Document(
                    id=document_id,
                    security_id=security_id,
                    source="finnhub",
                    source_record_id=article.external_id,
                    document_type=DocumentType.NEWS,
                    title=article.title,
                    url=article.url,
                    content_excerpt=(
                        article.body_text[:4000]
                        if article.body_text
                        else None
                    ),
                    published_at=article.published_at,
                    content_hash=article.content_hash,
                    retrieved_at=utc_now(),
                    knowledge_date=article.published_at.date(),
                )
                await self._document_sink.upsert_document(doc)
                result.items_written += 1
                result.new_document_ids.append(document_id)

            if latest_published is None or article.published_at > latest_published:
                latest_published = article.published_at

        if latest_published is not None:
            await self._watermarks.set_watermark(key, latest_published.isoformat())
        return result
