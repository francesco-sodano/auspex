from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TypeVar
from unittest.mock import AsyncMock

import pytest

from auspex.cli.bootstrap import BootstrapRunner, extraction_backfill_start
from auspex.config.loader import Universe
from auspex.extraction.channel_a import ChannelAExtractor
from auspex.extraction.relevance import news_is_relevant
from auspex.extraction.sections import Section, document_sections
from auspex.models.common import utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType, FilerProfile
from auspex.models.extraction import ChannelAExtraction, ChannelBDigest
from auspex.models.security import Security
from auspex.persistence.memory import (
    InMemoryBlobSink,
    InMemoryDocumentSink,
    InMemoryFundamentalSink,
    InMemoryFxSink,
    InMemoryPriceSink,
    InMemoryRepository,
    InMemoryWatermarkStore,
)
from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.steps import step_extract_channel_a, step_extract_channel_b

AS_OF = date(2026, 9, 18)
SOURCE = "Alpha expanded its data center cooling business."
SECURITY = Security(
    id="sec-a", ticker="AAA", cik="0000000001", name="Alpha Corp",
    cohort="test", filer_profile=FilerProfile.DOMESTIC,
)
TExtraction = TypeVar("TExtraction", ChannelAExtraction, ChannelBDigest)


class _ExtractionSink(InMemoryRepository[TExtraction]):
    async def find_by_cache_key(self, cache_key: str) -> TExtraction | None:
        return next((item for item in self.all() if item.cache_key == cache_key), None)


def _payload():
    return {
        "materiality": "HIGH", "sentiment": "POSITIVE", "guidance_direction": "NONE",
        "novelty": "NEW_INFORMATION", "extraction_confidence": "HIGH",
        "theme_claims": [{"theme_id": "cooling", "strength": "STRONG", "evidence_excerpt": SOURCE}],
        "risk_claims": [], "narrative_claims": [],
    }


def _extractor(sink, client):
    return ChannelAExtractor(
        openai_client=client, deployment="test", system_prompt="Extract verified claims.",
        model_version="test", taxonomy_version="test", sink=sink,
    )


async def _extract(extractor, source=SOURCE):
    return await extractor.extract(
        security_id=SECURITY.id, document_id="doc-a", content_hash="raw-unchanged",
        ticker="AAA", form_type="10-Q", sections=[Section(item="mda", text=source)],
        taxonomy_theme_ids=["cooling"],
    )


async def test_cache_tracks_processed_input_and_replaces_old_output_in_place():
    sink = _ExtractionSink[ChannelAExtraction]()
    client = AsyncMock()
    client.complete_json.return_value = json.dumps(_payload())
    extractor = _extractor(sink, client)

    first = await _extract(extractor)
    cached = await _extract(extractor)
    changed = await _extract(extractor, source=f"{SOURCE}\nA new substantive section.")

    assert first.input_fingerprint
    assert cached == first
    assert changed.id == first.id
    assert changed.input_fingerprint != first.input_fingerprint
    assert client.complete_json.await_count == 2
    assert extractor.cache_hits == 1
    assert len(sink.all()) == 1


async def test_legacy_header_only_extraction_is_not_reused():
    sink = _ExtractionSink[ChannelAExtraction]()
    client = AsyncMock()
    client.complete_json.return_value = json.dumps(_payload())
    extractor = _extractor(sink, client)
    legacy = extractor.parse_response(
        json.dumps({**_payload(), "theme_claims": []}),
        security_id=SECURITY.id, document_id="doc-a", content_hash="raw-unchanged",
    )
    await sink.upsert(legacy)

    refreshed = await _extract(extractor)

    assert refreshed.id == legacy.id
    assert refreshed.theme_claims[0].evidence_excerpt == SOURCE
    assert refreshed.input_fingerprint
    assert len(sink.all()) == 1


async def test_themes_and_spans_must_be_supported_by_the_source(caplog):
    sink = _ExtractionSink[ChannelAExtraction]()
    client = AsyncMock()
    payload = _payload()
    payload["theme_claims"].extend([
        {"theme_id": "invented", "strength": "STRONG", "evidence_excerpt": SOURCE},
        {"theme_id": "cooling", "strength": "STRONG", "evidence_excerpt": "An invented acquisition."},
    ])
    client.complete_json.return_value = json.dumps(payload)

    extraction = await _extract(_extractor(sink, client))

    assert len(extraction.theme_claims) == 1
    assert extraction.discarded_claim_count == 2
    assert "unsupported or ungrounded" in caplog.text


def _news(title="Alpha expands cooling capacity"):
    return Document(
        id="doc-news", security_id=SECURITY.id, source="finnhub", source_record_id="news-1",
        document_type=DocumentType.NEWS, title=title, content_excerpt=SOURCE,
        content_hash="news-hash", knowledge_date=AS_OF, retrieved_at=utc_now(),
    )


async def test_both_channels_extract_news_without_a_form_type_or_blob():
    doc = _news()
    documents = InMemoryDocumentSink()
    await documents.upsert_document(doc)
    channel_a = _ExtractionSink[ChannelAExtraction]()
    channel_b = _ExtractionSink[ChannelBDigest]()
    client = AsyncMock()
    client.complete_json.side_effect = [
        json.dumps(_payload()),
        json.dumps({
            "headline": "Cooling investment", "digest": SOURCE,
            "plain_summary": SOURCE, "plain_summary_evidence": [SOURCE],
        }),
    ]
    ctx = PipelineContext(
        universe=Universe(securities=[SECURITY]), as_of_date=AS_OF,
        user_id="owner", config={"taxonomy": {"taxonomy_version": "test", "themes": [{"id": "cooling"}]}},
        providers=PipelineProviders(openai_client=client),
        repos=PipelineRepos(
            document_sink=documents, blob_sink=InMemoryBlobSink(), price_sink=InMemoryPriceSink(),
            fundamental_sink=InMemoryFundamentalSink(), fx_sink=InMemoryFxSink(),
            watermarks=InMemoryWatermarkStore(), channel_a_sink=channel_a, channel_b_sink=channel_b,
        ),
        new_document_ids_by_security={SECURITY.id: [doc.id]},
    )

    await step_extract_channel_a(ctx, new_manifest(AS_OF))
    await step_extract_channel_b(ctx, new_manifest(AS_OF))

    assert len(channel_a.all()) == len(channel_b.all()) == 1
    assert channel_a.all()[0].theme_claims[0].theme_id == "cooling"
    assert not ctx.degraded_securities
    calls = client.complete_json.call_args_list
    assert calls[0].kwargs["max_tokens"] == 1500
    assert calls[1].kwargs["max_tokens"] == 2000
    assert json.loads(calls[0].kwargs["user_content"])["document"]["form_type"] == "NEWS"


@pytest.mark.parametrize("ticker,name", [("ON", "ON Semiconductor Corp"), ("NOW", "ServiceNow Inc")])
def test_generic_news_does_not_match_common_word_tickers(ticker, name):
    security = SECURITY.model_copy(update={"ticker": ticker, "name": name})
    assert not news_is_relevant(_news("Investors focus on the stock market right now"), security)
    assert news_is_relevant(_news(f"${ticker} announces results"), security)


def test_whole_filings_and_news_are_html_cleaned():
    document = _news().model_copy(update={"document_type": DocumentType.FORM_8K, "form_type": "8-K"})
    sections = document_sections(document, "<html><head><title>metadata</title></head><p>Real source.</p></html>")
    assert sections == [Section(item="full_document", text="Real source.")]


async def test_recovery_refreshes_used_warmup_interpretations_without_expanding_new_extraction(monkeypatch):
    floor = extraction_backfill_start(AS_OF)
    documents = InMemoryDocumentSink()
    channel_a = _ExtractionSink[ChannelAExtraction]()
    channel_b = _ExtractionSink[ChannelBDigest]()
    dates = {
        "current": floor,
        "previously-read": floor - timedelta(days=30),
        "old-unread": floor - timedelta(days=30),
        "too-old": floor - timedelta(days=181),
        "future": AS_OF + timedelta(days=1),
    }
    for document_id, knowledge_date in dates.items():
        await documents.upsert_document(_news().model_copy(update={
            "id": document_id, "knowledge_date": knowledge_date,
        }))
    for document_id in ("previously-read", "too-old"):
        await channel_b.upsert(ChannelBDigest(
            id=f"digest-{document_id}", security_id=SECURITY.id, document_id=document_id,
            content_hash="news-hash", model_version="test", headline="An older interpretation", digest=SOURCE,
        ))
    ctx = PipelineContext(
        universe=Universe(securities=[SECURITY]), as_of_date=AS_OF, user_id="owner", config={},
        repos=PipelineRepos(
            document_sink=documents, blob_sink=InMemoryBlobSink(), price_sink=InMemoryPriceSink(),
            fundamental_sink=InMemoryFundamentalSink(), fx_sink=InMemoryFxSink(),
            watermarks=InMemoryWatermarkStore(), channel_a_sink=channel_a, channel_b_sink=channel_b,
        ),
    )
    for name in ("step_extract_channel_a", "step_extract_channel_b"):
        monkeypatch.setattr(f"auspex.cli.bootstrap.{name}", AsyncMock())
    runner = BootstrapRunner(universe=ctx.universe, context_factory=lambda _date: ctx)
    await runner.extract_and_collect_fundamentals(ctx, include_fundamentals=False)
    assert set(ctx.new_document_ids_by_security[SECURITY.id]) == {"current", "previously-read"}
