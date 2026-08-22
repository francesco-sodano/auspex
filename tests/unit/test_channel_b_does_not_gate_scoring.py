"""Channel B failures must cost the explanation, never the score.

Channel B produces narrative digests, plain summaries and key quotes. It feeds
none of the six deterministic legs — Channel A does. Marking a security
degraded on a Channel B failure dropped it from scoring entirely, so a
malformed model response about *how to describe* a filing silently removed the
security's score, percentile and recommendation for the night.
"""

from __future__ import annotations

from datetime import date

import pytest

from auspex.models.common import content_hash, utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType, FilerProfile
from auspex.persistence.memory import (
    InMemoryBlobSink,
    InMemoryDocumentSink,
    InMemoryFundamentalSink,
    InMemoryFxSink,
    InMemoryPriceSink,
    InMemoryWatermarkStore,
)
from auspex.pipeline import steps as pipeline_steps
from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.manifest import new_manifest

AS_OF = date(2026, 8, 20)
SECURITY_ID = "sec-1"


class _Security:
    id = SECURITY_ID
    ticker = "SEC"
    cohort = "c"
    filer_profile = FilerProfile.DOMESTIC


class _Universe:
    securities = [_Security()]


class _DigestSink:
    def __init__(self) -> None:
        self._items: list = []

    def all(self) -> list:
        return list(self._items)

    async def find_by_cache_key(self, _cache_key):
        return None

    async def upsert(self, item) -> None:  # pragma: no cover - never reached here
        self._items.append(item)


class _ExplodingExtractor:
    """Stands in for a model response the parser cannot make sense of."""

    prompt_version = "digest-b-v2"

    def __init__(self, **_kwargs) -> None:
        pass

    async def extract(self, **_kwargs):
        raise ValueError("model returned prose where JSON was required")


@pytest.fixture
def context(monkeypatch) -> PipelineContext:
    monkeypatch.setattr(pipeline_steps, "ChannelBExtractor", _ExplodingExtractor)

    blob = InMemoryBlobSink()
    document_sink = InMemoryDocumentSink()
    text = "Item 7. Management's discussion of the period."
    blob.documents["blob/doc-1.txt"] = text
    document = Document(
        id="doc-1",
        security_id=SECURITY_ID,
        source="edgar",
        source_record_id="0000000000-26-000001",
        document_type=DocumentType.FORM_8K,
        form_type="8-K",
        filed_date=AS_OF,
        accession_number="0000000000-26-000001",
        url="https://example.invalid/doc-1",
        blob_path="blob/doc-1.txt",
        content_hash=content_hash(text),
        retrieved_at=utc_now(),
        knowledge_date=AS_OF,
    )
    document_sink._docs[document.id] = document

    repos = PipelineRepos(
        document_sink=document_sink,
        price_sink=InMemoryPriceSink(),
        fx_sink=InMemoryFxSink(),
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=blob,
        watermarks=InMemoryWatermarkStore(),
        channel_b_sink=_DigestSink(),
        narrative_sink=_DigestSink(),
    )
    ctx = PipelineContext(
        universe=_Universe(),
        config={"policy": {}},
        as_of_date=AS_OF,
        user_id="owner",
        repos=repos,
        providers=PipelineProviders(openai_client=object()),
    )
    ctx.new_document_ids_by_security[SECURITY_ID] = ["doc-1"]
    return ctx


@pytest.mark.asyncio
async def test_channel_b_failure_does_not_exclude_the_security_from_scoring(context):
    manifest = new_manifest(AS_OF)

    await pipeline_steps.step_extract_channel_b(context, manifest)

    assert SECURITY_ID in context.explanation_degraded_securities
    assert SECURITY_ID not in context.degraded_securities


@pytest.mark.asyncio
async def test_channel_b_failure_is_still_visible_on_the_manifest(context):
    """Not marking the score stale must not mean hiding the failure."""

    manifest = new_manifest(AS_OF)

    await pipeline_steps.step_extract_channel_b(context, manifest)

    checkpoint = manifest.step_by_name("EXTRACT_CHANNEL_B")
    assert checkpoint.status == "SUCCESS"
    assert checkpoint.degraded is True
    assert "failures=1" in checkpoint.detail


@pytest.mark.asyncio
async def test_a_derived_user_context_shares_both_degradation_sets(context):
    """The fan-out must not lose either signal when deriving a user context."""

    await pipeline_steps.step_extract_channel_b(context, new_manifest(AS_OF))
    derived = context.derive_for_user("user-bob")

    assert derived.explanation_degraded_securities is context.explanation_degraded_securities
    assert derived.degraded_securities is context.degraded_securities


class _StubNarrativeGenerator:
    prompt_version = "narrative-v2"

    def __init__(self, **_kwargs) -> None:
        pass

    async def generate(self, **_kwargs) -> str:
        return "A short explanation."


@pytest.mark.asyncio
async def test_narrate_reports_which_explanations_rest_on_degraded_evidence(context, monkeypatch):
    """Not gating the score must not mean hiding the thinner explanation.

    The security is still scored and still narrated — but the narrative was
    built without the Channel B digest that failed, and the run says so.
    """

    monkeypatch.setattr(pipeline_steps, "NarrativeGenerator", _StubNarrativeGenerator)
    await pipeline_steps.step_extract_channel_b(context, new_manifest(AS_OF))
    context.__dict__["_packages_by_security"] = {SECURITY_ID: {"security_id": SECURITY_ID}}

    manifest = new_manifest(AS_OF)
    await pipeline_steps.step_narrate(context, manifest)

    checkpoint = manifest.step_by_name("NARRATE")
    assert checkpoint.status == "SUCCESS"
    assert "generated=1" in checkpoint.detail
    assert "explanation_evidence_degraded=1" in checkpoint.detail
    assert checkpoint.degraded is True


@pytest.mark.asyncio
async def test_narrate_is_not_degraded_when_every_explanation_is_fully_evidenced(
    context, monkeypatch
):
    monkeypatch.setattr(pipeline_steps, "NarrativeGenerator", _StubNarrativeGenerator)
    context.__dict__["_packages_by_security"] = {SECURITY_ID: {"security_id": SECURITY_ID}}

    manifest = new_manifest(AS_OF)
    await pipeline_steps.step_narrate(context, manifest)

    checkpoint = manifest.step_by_name("NARRATE")
    assert checkpoint.detail == "generated=1"
    assert checkpoint.degraded is False
