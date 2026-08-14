"""Production-adapter integration tests (arc42 §6.1).

Unlike :mod:`tests.integration.test_pipeline_fixtures`, which exercises the
20-step pipeline against the in-memory test fixtures
(:mod:`auspex.persistence.memory`), this module wires
:class:`~auspex.pipeline.context.PipelineContext` against the *production*
adapters this codebase actually ships for Cosmos DB and Blob Storage
(:mod:`auspex.persistence.repositories`'s ``Cosmos*Sink``/``CosmosRepository``
classes and :class:`auspex.persistence.blob_client.BlobContext`), plus the
real :class:`auspex.providers.openai_provider.AzureOpenAIClient`. Only the
underlying Azure SDK objects are faked (:mod:`tests.integration.fake_production_adapters`)
— every line of *our* production adapter code runs for real, proving the
blocker this suite targets is fixed: Channel A/B extraction, narrative
generation, and every other step genuinely read/write through Cosmos
query/`.all()` and Blob `download_document_text`, not `.documents`/`.all()`
in-memory shortcuts.
"""

from __future__ import annotations

from datetime import date, timedelta

from auspex.models.common import content_hash, new_id, utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType, GuidanceLanguageShift, MdaToneShift
from auspex.models.policy import Recommendation
from auspex.models.portfolio import PortfolioProjection
from auspex.models.run import RunManifest
from auspex.models.scoring import LegChange, ScoreSnapshot
from auspex.persistence.blob_client import BlobContext
from auspex.persistence.repositories import (
    CosmosChannelAExtractionSink,
    CosmosChannelBDigestSink,
    CosmosDocumentSink,
    CosmosFundamentalSink,
    CosmosFxSink,
    CosmosNarrativeSink,
    CosmosPriceSink,
    CosmosRepository,
    CosmosWatermarkStore,
)
from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.runner import run_nightly_pipeline
from auspex.pipeline.steps import step_extract_channel_a, step_extract_channel_b, step_narrate
from auspex.providers.openai_provider import AzureOpenAIClient
from tests.integration.fake_production_adapters import (
    FakeBlobServiceClient,
    FakeCosmosContext,
    QueuedFakeChatCompletions,
)

CHANNEL_A_JSON = """
{
  "materiality": "HIGH",
  "sentiment": "POSITIVE",
  "guidance_direction": "RAISED",
  "novelty": "NEW_INFORMATION",
  "theme_claims": [],
  "risk_claims": [],
  "narrative_claims": [],
  "extraction_confidence": "HIGH"
}
"""

CHANNEL_B_JSON = """
{
  "headline": "Data center demand reiterated as very strong",
  "digest": "Management reiterated that demand for data center accelerators remains very strong across customers.",
  "key_quotes": [],
  "management_claims": ["Demand remains very strong"],
  "unanswered_questions": [],
  "comparative": {
    "prior_document_id": null,
    "risk_factors_added": [],
    "risk_factors_removed": [],
    "risk_factors_reworded": [],
    "guidance_language_shift": "FIRMED",
    "mda_tone_shift": "MORE_CONFIDENT"
  }
}
"""

NARRATIVE_TEXT = "NVDA's composite improved this week on continued strong thesis-linkage evidence."


def build_production_context(universe, config_bundle, as_of_date: date, *, with_openai: bool = True):
    """A :class:`PipelineContext` wired entirely against production-shaped
    adapters: Cosmos-backed sinks over a faked Cosmos SDK boundary, a real
    :class:`BlobContext` over a faked Blob SDK boundary, and (optionally) a
    real :class:`AzureOpenAIClient` over a faked chat-completions call."""

    cosmos = FakeCosmosContext()

    blob = BlobContext()
    blob._client = FakeBlobServiceClient()  # noqa: SLF001 - swapping only the SDK boundary

    repos = PipelineRepos(
        document_sink=CosmosDocumentSink(cosmos),
        price_sink=CosmosPriceSink(cosmos),
        fx_sink=CosmosFxSink(cosmos),
        fundamental_sink=CosmosFundamentalSink(cosmos),
        blob_sink=blob,
        watermarks=CosmosWatermarkStore(cosmos),
        channel_a_sink=CosmosChannelAExtractionSink(cosmos),
        channel_b_sink=CosmosChannelBDigestSink(cosmos),
        narrative_sink=CosmosNarrativeSink(cosmos),
        score_repo=CosmosRepository(cosmos, "scores", ScoreSnapshot),
        leg_change_repo=CosmosRepository(cosmos, "leg_changes", LegChange),
        recommendation_repo=CosmosRepository(cosmos, "recommendations", Recommendation),
        run_repo=CosmosRepository(cosmos, "runs", RunManifest),
        portfolio_projection_repo=CosmosRepository(cosmos, "portfolio_projection", PortfolioProjection),
    )

    openai_client = None
    fake_chat = None
    if with_openai:
        openai_client = AzureOpenAIClient(endpoint="https://aoai-test.openai.azure.com/", api_version="2024-10-21")
        fake_chat = QueuedFakeChatCompletions(
            channel_a_json=CHANNEL_A_JSON, channel_b_json=CHANNEL_B_JSON, narrative_text=NARRATIVE_TEXT
        )
        openai_client._client.chat.completions.create = fake_chat.create  # noqa: SLF001

    providers = PipelineProviders(openai_client=openai_client)
    ctx = PipelineContext(
        universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos,
        providers=providers,
    )
    return ctx, cosmos, blob, fake_chat


async def _seed_document(
    ctx: PipelineContext, *, security_id: str, form_type: str, filed_date: date, raw_text: str
) -> Document:
    document_id = new_id()
    blob_path = await ctx.repos.blob_sink.upload_document_blob(security_id, document_id, "htm", raw_text)
    doc = Document(
        id=document_id,
        security_id=security_id,
        source="edgar",
        source_record_id=f"acc-{document_id}",
        document_type=DocumentType(form_type),
        form_type=form_type,
        accession_number=f"acc-{document_id}",
        filed_date=filed_date,
        blob_path=blob_path,
        content_hash=content_hash(raw_text),
        retrieved_at=utc_now(),
        knowledge_date=filed_date,
    )
    await ctx.repos.document_sink.upsert_document(doc)
    return doc


class TestChannelAExtractionAgainstProductionAdapters:
    async def test_reads_document_and_blob_via_cosmos_and_blob_adapters(self, universe, config_bundle):
        as_of_date = date(2026, 8, 8)
        ctx, _, _, fake_chat = build_production_context(universe, config_bundle, as_of_date)
        nvda = universe.by_ticker()["NVDA"]

        doc = await _seed_document(
            ctx,
            security_id=nvda.id,
            form_type="8-K",
            filed_date=as_of_date - timedelta(days=1),
            raw_text="Demand for our data center accelerators remains very strong across every major customer.",
        )
        ctx.new_document_ids_by_security[nvda.id] = [doc.id]

        manifest = new_manifest(as_of_date)
        await step_extract_channel_a(ctx, manifest)

        cp = manifest.step_by_name("EXTRACT_CHANNEL_A")
        assert cp.status == "SUCCESS"
        assert cp.detail == "extracted=1"

        extractions = await ctx.repos.channel_a_sink.all()
        assert len(extractions) == 1
        extraction = extractions[0]
        assert extraction.security_id == nvda.id
        assert extraction.document_id == doc.id
        assert extraction.content_hash == doc.content_hash

        # deployment/model/prompt are configured, not the old hard-coded
        # nonexistent "channel-a" deployment / empty system prompt
        from auspex.settings import get_settings

        settings = get_settings()
        assert fake_chat.calls[0]["model"] == settings.aoai_deployment_extraction
        assert len(fake_chat.calls) == 1
        system_prompt = fake_chat.calls[0]["messages"][0]["content"]
        assert "Channel A" in system_prompt  # loaded from prompts/extract_channel_a_v1.md, not ""

    async def test_cache_hit_on_rerun_makes_no_second_llm_call(self, universe, config_bundle):
        """Cache key = content_hash + model_version + prompt_version + schema_version +
        taxonomy_version (arc42 §5.4) — re-running EXTRACT_CHANNEL_A for the
        same document must be a pure Cosmos cache hit, never a second LLM call.
        """

        as_of_date = date(2026, 8, 8)
        ctx, _, _, fake_chat = build_production_context(universe, config_bundle, as_of_date)
        nvda = universe.by_ticker()["NVDA"]
        doc = await _seed_document(
            ctx, security_id=nvda.id, form_type="8-K", filed_date=as_of_date - timedelta(days=1), raw_text="Body."
        )
        ctx.new_document_ids_by_security[nvda.id] = [doc.id]

        manifest = new_manifest(as_of_date)
        await step_extract_channel_a(ctx, manifest)
        await step_extract_channel_a(ctx, manifest)

        assert len(fake_chat.calls) == 1  # second run found the cached extraction via Cosmos, no re-call
        assert len(await ctx.repos.channel_a_sink.all()) == 1  # and did not duplicate the row


class TestChannelBExtractionAgainstProductionAdapters:
    async def test_persists_digest_with_comparative_against_prior_filing(self, universe, config_bundle):
        as_of_date = date(2026, 8, 8)
        ctx, _, _, fake_chat = build_production_context(universe, config_bundle, as_of_date)
        nvda = universe.by_ticker()["NVDA"]

        prior_doc = await _seed_document(
            ctx,
            security_id=nvda.id,
            form_type="8-K",
            filed_date=as_of_date - timedelta(days=90),
            raw_text="Prior quarter: demand was solid but decelerating slightly.",
        )
        new_doc = await _seed_document(
            ctx,
            security_id=nvda.id,
            form_type="8-K",
            filed_date=as_of_date - timedelta(days=1),
            raw_text="This quarter: demand for data center accelerators is accelerating again.",
        )
        # only the new filing is "today's" evidence — the prior one is
        # already-known history the comparative diff reads against.
        ctx.new_document_ids_by_security[nvda.id] = [new_doc.id]

        manifest = new_manifest(as_of_date)
        await step_extract_channel_b(ctx, manifest)

        cp = manifest.step_by_name("EXTRACT_CHANNEL_B")
        assert cp.status == "SUCCESS"
        assert cp.detail == "extracted=1"

        digests = await ctx.repos.channel_b_sink.all()
        assert len(digests) == 1
        digest = digests[0]
        assert digest.document_id == new_doc.id
        assert digest.comparative is not None
        assert digest.comparative.guidance_language_shift == GuidanceLanguageShift.FIRMED
        assert digest.comparative.mda_tone_shift == MdaToneShift.MORE_CONFIDENT

        # the LLM call actually received the prior document's sections
        user_content = fake_chat.calls[0]["messages"][1]["content"]
        assert "decelerating slightly" in user_content
        assert prior_doc.id != new_doc.id


class TestNarrateAgainstProductionAdapters:
    async def _seed_narrate_scratch_state(
        self, ctx: PipelineContext, security_id: str, as_of_date: date
    ) -> ScoreSnapshot:
        package = {"security_id": security_id, "as_of_date": as_of_date.isoformat(), "composite": "0.42"}
        snapshot = ScoreSnapshot(
            id=f"{security_id}:{as_of_date.isoformat()}",
            security_id=security_id,
            as_of_date=as_of_date,
            config_version_id="test-config",
            cohort_used="semi-compute",
            cohort_confidence="HIGH",
            filer_profile="DOMESTIC",
            coverage="0.5",
            legs={},
            composite="0.42",
            percentile=80,
            package_fingerprint="sha256:deadbeef",
            max_knowledge_date=as_of_date,
        )
        await ctx.repos.score_repo.upsert(snapshot)
        ctx.__dict__["_packages_by_security"] = {security_id: package}
        ctx.__dict__["_snapshots"] = [snapshot]
        ctx.__dict__["_leg_changes"] = []
        return snapshot

    async def test_generates_and_persists_narrative_onto_score_snapshot(self, universe, config_bundle):
        as_of_date = date(2026, 8, 8)
        ctx, _, _, fake_chat = build_production_context(universe, config_bundle, as_of_date)
        nvda = universe.by_ticker()["NVDA"]
        await self._seed_narrate_scratch_state(ctx, nvda.id, as_of_date)

        manifest = new_manifest(as_of_date)
        await step_narrate(ctx, manifest)

        cp = manifest.step_by_name("NARRATE")
        assert cp.status == "SUCCESS"
        assert cp.detail == "generated=1"

        stored = await ctx.repos.score_repo.get(f"{nvda.id}:{as_of_date.isoformat()}", partition_key=nvda.id)
        assert stored is not None
        assert stored.narrative == NARRATIVE_TEXT
        assert stored.narrative_model_version is not None
        assert len(fake_chat.calls) == 1

    async def test_rerun_is_a_pure_cache_hit(self, universe, config_bundle):
        as_of_date = date(2026, 8, 8)
        ctx, _, _, fake_chat = build_production_context(universe, config_bundle, as_of_date)
        nvda = universe.by_ticker()["NVDA"]
        await self._seed_narrate_scratch_state(ctx, nvda.id, as_of_date)

        manifest = new_manifest(as_of_date)
        await step_narrate(ctx, manifest)
        # re-seed the identical scratch state (as a fresh WRITE_SNAPSHOT would
        # for an unchanged day) and run NARRATE again
        await self._seed_narrate_scratch_state(ctx, nvda.id, as_of_date)
        await step_narrate(ctx, manifest)

        assert len(fake_chat.calls) == 1  # second call was a package_fingerprint cache hit, no re-call


class TestFullPipelineAgainstProductionAdapters:
    async def test_all_20_steps_execute_without_error_against_cosmos_and_blob_adapters(
        self, universe, config_bundle
    ):
        """No LLM configured here (extraction/narrate correctly SKIP), but
        every other step must genuinely read/write through the Cosmos/Blob
        production adapters — this is the end-to-end regression test for
        the original blocker (`.all()`/`.documents` against adapters that
        don't expose them silently reading zero documents).
        """

        as_of_date = date(2026, 8, 8)
        ctx, _, _, _ = build_production_context(universe, config_bundle, as_of_date, with_openai=False)

        manifest = await run_nightly_pipeline(ctx)

        from auspex.models.run import PIPELINE_STEPS

        assert [s.step for s in manifest.steps] == PIPELINE_STEPS
        assert all(s.status in ("SUCCESS", "SKIPPED") for s in manifest.steps)

        extract_a = manifest.step_by_name("EXTRACT_CHANNEL_A")
        assert extract_a.status == "SKIPPED"  # no LLM configured — degrades, does not fail

        # WRITE_SNAPSHOT/RUN_POLICY genuinely persisted through the Cosmos adapters
        scores = await ctx.repos.score_repo.all()
        assert len(scores) == len(universe.securities)
        recommendations = await ctx.repos.recommendation_repo.all()
        assert len(recommendations) == len(universe.securities)

    async def test_rerunning_same_date_against_production_adapters_is_idempotent(self, universe, config_bundle):
        as_of_date = date(2026, 8, 8)
        ctx1, cosmos, blob, _ = build_production_context(universe, config_bundle, as_of_date, with_openai=False)
        manifest1 = await run_nightly_pipeline(ctx1)
        scores_after_first = await ctx1.repos.score_repo.all()

        # re-run against the *same* underlying Cosmos/Blob fakes (a fresh
        # PipelineContext, matching how a real process re-invocation shares
        # the same Cosmos account/containers across runs)
        repos2 = PipelineRepos(
            document_sink=CosmosDocumentSink(cosmos),
            price_sink=CosmosPriceSink(cosmos),
            fx_sink=CosmosFxSink(cosmos),
            fundamental_sink=CosmosFundamentalSink(cosmos),
            blob_sink=blob,
            watermarks=CosmosWatermarkStore(cosmos),
            channel_a_sink=CosmosChannelAExtractionSink(cosmos),
            channel_b_sink=CosmosChannelBDigestSink(cosmos),
            narrative_sink=CosmosNarrativeSink(cosmos),
            score_repo=CosmosRepository(cosmos, "scores", ScoreSnapshot),
            leg_change_repo=CosmosRepository(cosmos, "leg_changes", LegChange),
            recommendation_repo=CosmosRepository(cosmos, "recommendations", Recommendation),
            run_repo=CosmosRepository(cosmos, "runs", RunManifest),
            portfolio_projection_repo=CosmosRepository(cosmos, "portfolio_projection", PortfolioProjection),
        )
        ctx2 = PipelineContext(
            universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos2
        )
        manifest2 = await run_nightly_pipeline(ctx2)
        scores_after_second = await ctx2.repos.score_repo.all()

        assert manifest1.status == manifest2.status
        assert len(scores_after_second) == len(scores_after_first)  # upsert-on-id, never duplicated

        nvda = universe.by_ticker()["NVDA"]
        score1 = await ctx1.repos.score_repo.get(f"{nvda.id}:{as_of_date.isoformat()}", partition_key=nvda.id)
        score2 = await ctx2.repos.score_repo.get(f"{nvda.id}:{as_of_date.isoformat()}", partition_key=nvda.id)
        assert score1.package_fingerprint == score2.package_fingerprint
        assert score1.composite == score2.composite
