"""The 20 nightly pipeline steps (arc42 §6.1).

Each step is a pure orchestration function over :class:`PipelineContext`; the
actual domain logic lives in :mod:`auspex.collectors`, :mod:`auspex.scoring`,
:mod:`auspex.policy`, :mod:`auspex.portfolio`, and :mod:`auspex.narrative`. A
missing optional dependency (no provider/LLM client configured) marks the
step SKIPPED rather than FAILED — arc42 §6.1: provider failures degrade
coverage, they do not abort the run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from auspex.collectors.filing_collector import FilingCollector
from auspex.collectors.fundamental_collector import FundamentalCollector
from auspex.collectors.fx_collector import FxCollector
from auspex.collectors.insider_collector import InsiderCollector
from auspex.collectors.news_collector import NewsCollector
from auspex.collectors.price_collector import PriceCollector
from auspex.extraction.cache import channel_a_cache_key, channel_b_cache_key
from auspex.extraction.channel_a import ChannelAExtractor
from auspex.extraction.channel_b import ChannelBExtractor
from auspex.extraction.sections import WHOLE_DOCUMENT_FORMS, target_sections
from auspex.marketdata.quarantine import exclude_quarantined
from auspex.models.common import utc_now
from auspex.models.document import Document
from auspex.models.enums import Action, CohortConfidence, Direction, FilerProfile, LegName
from auspex.models.policy import RecommendationDisposition
from auspex.models.run import RunManifest
from auspex.models.scoring import LegChange, LegResult, ScoreSnapshot
from auspex.narrative.generator import NarrativeGenerator
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.feature_builder import (
    WeightsConfig,
    build_attention_events,
    build_fundamental_health_inputs,
    build_insider_events,
    build_narrative_events,
    build_thesis_linkage_events,
    build_valuation_metrics,
)
from auspex.pipeline.manifest import complete_step, skip_step, start_step
from auspex.pipeline.prompts import load_prompt
from auspex.pipeline.repo_access import fetch_all, read_blob_text
from auspex.policy.assertions import run_post_run_assertions
from auspex.policy.engine import load_policy_thresholds
from auspex.scoring.composite import decompose_leg_delta
from auspex.scoring.coverage import is_stale
from auspex.scoring.engine import SecurityScoringInput, build_cohort_scopes, score_universe
from auspex.scoring.legs import (
    FundamentalHealthInputs,
    ValuationMetrics,
    attention_acceleration,
    fundamental_health,
    narrative_premium,
    smart_money,
    thesis_linkage,
    valuation_brake,
)
from auspex.scoring.normalize import percentile_rank
from auspex.scoring.sessions import (
    contiguous_weakening_streak,
    normalise_calendar,
    nth_prior_session,
    prior_sessions,
    sessions_between,
)
from auspex.settings import get_settings

logger = logging.getLogger("auspex.pipeline")

#: Direction compares today's composite against roughly one trading week back.
#: Expressed in trading sessions so a market holiday cannot shift the reference
#: point onto a day that never had a score row.
DIRECTION_LOOKBACK_SESSIONS = 5


async def step_start_run(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "START_RUN")
    complete_step(manifest, "START_RUN", detail=f"lease acquired for {ctx.as_of_date.isoformat()}")


async def step_collect_prices(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_PRICES")
    if ctx.providers.price_provider is None:
        skip_step(manifest, "COLLECT_PRICES", detail="no price provider configured")
        return
    collector = PriceCollector(ctx.providers.price_provider, ctx.repos.price_sink, ctx.repos.watermarks)
    default_since = ctx.as_of_date - timedelta(days=7)
    latest_as_of = getattr(ctx.repos.price_sink, "latest_as_of", None)
    cached = {}
    if latest_as_of is not None:
        cached_rows = await latest_as_of(
            ctx.as_of_date,
            [security.id for security in ctx.universe.securities],
        )
        cached = {row.security_id: row for row in cached_rows}
    degraded = 0
    for sec in ctx.universe.securities:
        result = await collector.collect(sec.id, sec.ticker, default_since)
        if result.degraded:
            cached_row = cached.get(sec.id)
            cache_age = (
                (ctx.as_of_date - cached_row.session_date).days
                if cached_row is not None
                else None
            )
            if cache_age is None or cache_age > 4:
                degraded += 1
                ctx.degraded_securities.add(sec.id)
    complete_step(manifest, "COLLECT_PRICES", detail=f"degraded={degraded}", degraded=degraded > 0)


async def step_collect_fx(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_FX")
    if ctx.providers.fx_provider is None:
        skip_step(manifest, "COLLECT_FX", detail="no FX provider configured")
        return
    collector = FxCollector(ctx.providers.fx_provider, ctx.repos.fx_sink, ctx.repos.watermarks)
    result = await collector.collect(
        ctx.as_of_date - timedelta(days=7),
        pairs=tuple(
            ctx.config["weights"].get(
                "valuation_fx_pairs",
                ["USDCHF"],
            )
        ),
    )
    complete_step(manifest, "COLLECT_FX", detail=f"written={result.items_written}", degraded=result.degraded)


async def step_collect_filings(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_FILINGS")
    if ctx.providers.edgar_client is None:
        skip_step(manifest, "COLLECT_FILINGS", detail="no EDGAR client configured")
        return
    collector = FilingCollector(
        ctx.providers.edgar_client, ctx.repos.document_sink, ctx.repos.blob_sink, ctx.repos.watermarks
    )
    degraded = 0
    for sec in ctx.universe.securities:
        result = await collector.collect(sec.id, sec.cik)
        ctx.new_document_ids_by_security.setdefault(sec.id, []).extend(result.new_document_ids)
        if result.degraded:
            degraded += 1
    complete_step(manifest, "COLLECT_FILINGS", detail=f"degraded={degraded}", degraded=degraded > 0)


async def step_collect_insiders(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_INSIDERS")
    if ctx.providers.edgar_client is None:
        skip_step(manifest, "COLLECT_INSIDERS", detail="no EDGAR client configured")
        return
    collector = InsiderCollector(ctx.providers.edgar_client, ctx.repos.document_sink, ctx.repos.watermarks)
    degraded = 0
    for sec in ctx.universe.securities:
        if sec.filer_profile == FilerProfile.FPI:
            continue  # FPI files no Form 4 (arc42 §5.2)
        result = await collector.collect(sec.id, sec.cik)
        ctx.new_document_ids_by_security.setdefault(sec.id, []).extend(result.new_document_ids)
        if result.degraded:
            degraded += 1
    complete_step(manifest, "COLLECT_INSIDERS", detail=f"degraded={degraded}", degraded=degraded > 0)


async def step_collect_news(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_NEWS")
    if ctx.providers.news_provider is None:
        skip_step(manifest, "COLLECT_NEWS", detail="no news provider configured")
        return
    collector = NewsCollector(ctx.providers.news_provider, ctx.repos.document_sink, ctx.repos.watermarks)
    default_since = datetime.combine(ctx.as_of_date - timedelta(days=7), datetime.min.time())
    degraded = 0
    for sec in ctx.universe.securities:
        result = await collector.collect(sec.id, sec.ticker, default_since)
        ctx.new_document_ids_by_security.setdefault(sec.id, []).extend(result.new_document_ids)
        if result.degraded:
            degraded += 1
    complete_step(manifest, "COLLECT_NEWS", detail=f"degraded={degraded}", degraded=degraded > 0)


async def step_collect_fundamentals(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "COLLECT_FUNDAMENTALS")
    if ctx.providers.edgar_client is None:
        skip_step(manifest, "COLLECT_FUNDAMENTALS", detail="no EDGAR client configured")
        return
    collector = FundamentalCollector(ctx.providers.edgar_client, ctx.repos.fundamental_sink, ctx.repos.watermarks)
    all_docs = await fetch_all(ctx.repos.document_sink)
    degraded = 0
    for sec in ctx.universe.securities:
        new_ids = ctx.new_document_ids_by_security.get(sec.id, [])
        new_docs = [d for d in all_docs if d.id in new_ids]
        accessions = {
            d.accession_number
            for d in new_docs
            if d.accession_number and d.document_type.value in ("10-K", "10-Q", "20-F")
        }
        if not accessions:
            continue
        result = await collector.collect(sec.id, sec.cik, accessions)
        if result.degraded:
            degraded += 1
    complete_step(manifest, "COLLECT_FUNDAMENTALS", detail=f"degraded={degraded}", degraded=degraded > 0)


async def step_extract_channel_a(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "EXTRACT_CHANNEL_A")
    if ctx.providers.openai_client is None or ctx.repos.channel_a_sink is None:
        skip_step(manifest, "EXTRACT_CHANNEL_A", detail="no LLM/sink configured")
        return
    settings = get_settings()
    extractor = ChannelAExtractor(
        openai_client=ctx.providers.openai_client,
        deployment=settings.aoai_deployment_extraction,
        system_prompt=load_prompt(ChannelAExtractor.prompt_version),
        model_version=settings.aoai_deployment_extraction,
        taxonomy_version=ctx.config["taxonomy"]["taxonomy_version"],
        sink=ctx.repos.channel_a_sink,
    )
    taxonomy_ids = [t["id"] for t in ctx.config["taxonomy"]["themes"]]
    documents_by_id = {d.id: d for d in await fetch_all(ctx.repos.document_sink)}

    count = 0
    failures = 0
    for sec in ctx.universe.securities:
        for doc_id in ctx.new_document_ids_by_security.get(sec.id, []):
            doc = documents_by_id.get(doc_id)
            if doc is None or doc.form_type is None:
                continue
            cache_key = channel_a_cache_key(
                security_id=sec.id,
                content_hash=doc.content_hash,
                model_version=settings.aoai_deployment_extraction,
                prompt_version=extractor.prompt_version,
                schema_version=extractor.schema_version,
                taxonomy_version=ctx.config["taxonomy"]["taxonomy_version"],
            )
            if await ctx.repos.channel_a_sink.find_by_cache_key(cache_key):
                continue
            raw_text = await read_blob_text(ctx.repos.blob_sink, doc.blob_path)
            if doc.form_type in WHOLE_DOCUMENT_FORMS:
                sections = [_whole_document_section(raw_text)]
            else:
                sections = target_sections(doc.form_type, raw_text)
            if not sections:
                continue
            try:
                await extractor.extract(
                    security_id=sec.id,
                    document_id=doc.id,
                    content_hash=doc.content_hash,
                    ticker=sec.ticker,
                    form_type=doc.form_type,
                    sections=sections,
                    taxonomy_theme_ids=taxonomy_ids,
                )
                count += 1
            except Exception as exc:  # noqa: BLE001 - one malformed model response must not abort the universe
                failures += 1
                ctx.degraded_securities.add(sec.id)
                logger.error(
                    "Channel A extraction failed for %s document %s: %s",
                    sec.ticker,
                    doc.id,
                    exc,
                )
    complete_step(
        manifest,
        "EXTRACT_CHANNEL_A",
        detail=f"extracted={count}" + (f"; failures={failures}" if failures else ""),
        degraded=failures > 0,
    )


def _whole_document_section(text: str):
    from auspex.extraction.sections import Section

    return Section(item="full_document", text=text)


def _find_prior_comparable_document(
    all_docs: list[Document], *, security_id: str, form_type: str, before: date | None
) -> Document | None:
    """Latest prior filing of the same ``form_type`` for ``security_id``,
    strictly before ``before`` (arc42 §5.4 "comparative diff against the
    prior comparable filing"). ``None`` when there is nothing to diff
    against — Channel B still runs, just without a comparative section."""

    candidates = [
        d
        for d in all_docs
        if d.security_id == security_id
        and d.form_type == form_type
        and d.filed_date is not None
        and (before is None or d.filed_date < before)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.filed_date)


async def step_extract_channel_b(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "EXTRACT_CHANNEL_B")
    if ctx.providers.openai_client is None or ctx.repos.channel_b_sink is None:
        skip_step(manifest, "EXTRACT_CHANNEL_B", detail="no LLM/sink configured")
        return
    settings = get_settings()
    extractor = ChannelBExtractor(
        openai_client=ctx.providers.openai_client,
        deployment=settings.aoai_deployment_extraction,
        system_prompt=load_prompt(ChannelBExtractor.prompt_version),
        model_version=settings.aoai_deployment_extraction,
        sink=ctx.repos.channel_b_sink,
    )
    all_docs = await fetch_all(ctx.repos.document_sink)
    documents_by_id = {d.id: d for d in all_docs}
    semaphore = asyncio.Semaphore(max(settings.extraction_concurrency, 1))

    async def extract_one(sec, doc):
        async with semaphore:
            cache_key = channel_b_cache_key(
                security_id=sec.id,
                content_hash=doc.content_hash,
                model_version=settings.aoai_deployment_extraction,
                prompt_version=extractor.prompt_version,
            )
            if await ctx.repos.channel_b_sink.find_by_cache_key(cache_key):
                return False, None
            raw_text = await read_blob_text(ctx.repos.blob_sink, doc.blob_path)
            if doc.form_type in WHOLE_DOCUMENT_FORMS:
                sections = [_whole_document_section(raw_text)]
            else:
                sections = target_sections(doc.form_type, raw_text)
            if not sections:
                return False, None

            prior_sections = None
            prior_doc = _find_prior_comparable_document(
                all_docs, security_id=sec.id, form_type=doc.form_type, before=doc.filed_date
            )
            if prior_doc is not None:
                prior_text = await read_blob_text(ctx.repos.blob_sink, prior_doc.blob_path)
                if doc.form_type in WHOLE_DOCUMENT_FORMS:
                    prior_sections = [_whole_document_section(prior_text)]
                else:
                    prior_sections = target_sections(doc.form_type, prior_text) or None

            try:
                await extractor.extract(
                    security_id=sec.id,
                    document_id=doc.id,
                    content_hash=doc.content_hash,
                    ticker=sec.ticker,
                    form_type=doc.form_type,
                    sections=sections,
                    prior_sections=prior_sections,
                )
                return True, None
            except Exception as exc:  # noqa: BLE001 - one malformed model response must not abort the universe
                # Deliberately *not* ctx.degraded_securities: Channel B feeds
                # narratives, digests and plain summaries, never one of the six
                # legs. Marking the security degraded here excluded it from
                # scoring entirely — the user lost their score because the
                # explanation failed, which inverts the dependency.
                logger.error(
                    "Channel B extraction failed for %s document %s: %s",
                    sec.ticker,
                    doc.id,
                    exc,
                )
                return False, sec.id

    work = [
        (sec, doc)
        for sec in ctx.universe.securities
        for doc_id in ctx.new_document_ids_by_security.get(sec.id, [])
        if (doc := documents_by_id.get(doc_id)) is not None
        and doc.form_type is not None
    ]
    results = await asyncio.gather(
        *(extract_one(sec, doc) for sec, doc in work)
    )
    count = sum(1 for extracted, _failed in results if extracted)
    failed_security_ids = {
        failed for _extracted, failed in results if failed is not None
    }
    ctx.explanation_degraded_securities.update(failed_security_ids)
    failures = sum(1 for _extracted, failed in results if failed is not None)
    complete_step(
        manifest,
        "EXTRACT_CHANNEL_B",
        detail=f"extracted={count}" + (f"; failures={failures}" if failures else ""),
        degraded=failures > 0,
    )


def _cohort_confidence_ok(confidence: CohortConfidence) -> bool:
    return confidence in (CohortConfidence.HIGH, CohortConfidence.MEDIUM)


async def step_compute_raw_legs(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Compute all six raw legs per non-stale security (arc42 §5.5).

    Fundamental-health ratios may use a consistent native reporting currency.
    Valuation needs one common currency to be comparable across peers, so a
    non-USD reporter is converted with point-in-time authoritative FX when an
    ``FxConverter`` is available and otherwise has its valuation leg marked
    *structurally not applicable* rather than silently failing coverage.
    """

    start_step(manifest, "COMPUTE_RAW_LEGS")
    weights_cfg = WeightsConfig.from_yaml(ctx.config["weights"])
    xbrl_concepts = ctx.config["xbrl_concepts"]
    fx_converter = ctx.__dict__.get("_fx_converter")
    if fx_converter is None:
        from auspex.currency.table import PointInTimeFxTable

        fx_converter = PointInTimeFxTable(
            await fetch_all(ctx.repos.fx_sink)
        )
        ctx.__dict__["_fx_converter"] = fx_converter

    documents_by_security: dict[str, list] = {}
    documents = ctx.__dict__.get("_preloaded_documents")
    if documents is None:
        documents = await fetch_all(ctx.repos.document_sink)
    for doc in documents:
        documents_by_security.setdefault(doc.security_id, []).append(doc)

    all_extractions = ctx.__dict__.get("_preloaded_extractions")
    if all_extractions is None:
        all_extractions = await fetch_all(ctx.repos.channel_a_sink) if ctx.repos.channel_a_sink is not None else []
    all_fundamentals = ctx.__dict__.get("_preloaded_fundamentals")
    if all_fundamentals is None:
        all_fundamentals = await fetch_all(ctx.repos.fundamental_sink)

    latest_prices_usd = await _latest_prices_usd(ctx)
    # arc42 §5.5 "Staleness exclusion": price age in *observed trading sessions*.
    # Unioned with this run's degraded set (a price that could not be refreshed,
    # or a Channel A extraction that raised — Channel A feeds the legs, so its
    # failure genuinely degrades the score). Channel B failures are deliberately
    # absent: they cost the user an explanation, never a score.
    stale_ids = await _stale_security_ids(ctx)

    def _shares_outstanding(fundamentals: list, as_of: date) -> Decimal | None:
        aliases = xbrl_concepts["concepts"]["shares_outstanding"]
        values = []
        for snap in fundamentals:
            if snap.filed > as_of:
                continue
            for fact in snap.facts:
                if (
                    fact.concept in aliases
                    and fact.filed <= as_of
                    and fact.unit == "shares"
                ):
                    values.append((fact.end, Decimal(fact.value)))
        if not values:
            return None
        values.sort(key=lambda t: t[0])
        return values[-1][1]

    per_security_state: dict[str, dict] = {}
    valuation_metrics_by_security: dict[str, ValuationMetrics] = {}
    fundamental_inputs_by_security: dict[str, FundamentalHealthInputs] = {}
    fx_unavailable_ids: set[str] = set()

    for sec in ctx.universe.securities:
        docs = documents_by_security.get(sec.id, [])
        documents_by_id = {d.id: d for d in docs}
        extractions = [e for e in all_extractions if e.security_id == sec.id]
        fundamentals = [s for s in all_fundamentals if s.security_id == sec.id]

        thesis_events = build_thesis_linkage_events(extractions, documents_by_id, weights_cfg, ctx.as_of_date)
        attention_events = build_attention_events(extractions, documents_by_id, weights_cfg, ctx.as_of_date)
        narrative_events = build_narrative_events(extractions, documents_by_id, weights_cfg, ctx.as_of_date)
        insider_events = build_insider_events(docs, ctx.as_of_date)
        fundamental_inputs = build_fundamental_health_inputs(
            fundamentals, xbrl_concepts, weights_cfg.roic_tax_rate, ctx.as_of_date
        )
        fundamental_inputs_by_security[sec.id] = fundamental_inputs

        # market_cap = latest close price x shares outstanding (both point-in-time,
        # arc42 §5.3), used by both smart_money's denominator and valuation_brake's EV.
        price = latest_prices_usd.get(sec.id)
        shares = _shares_outstanding(fundamentals, ctx.as_of_date)
        market_cap = (price * shares) if (price is not None and shares is not None) else None

        valuation = build_valuation_metrics(
            market_cap, fundamentals, xbrl_concepts, ctx.as_of_date, fx_converter=fx_converter
        )
        valuation_metrics_by_security[sec.id] = valuation.metrics
        if valuation.fx_unavailable:
            fx_unavailable_ids.add(sec.id)

        leg_raw: dict[LegName, Decimal | None] = {
            LegName.THESIS_LINKAGE: thesis_linkage(thesis_events, weights_cfg.recency_half_life_days),
            LegName.ATTENTION_ACCELERATION: attention_acceleration(attention_events),
        }
        if sec.filer_profile == FilerProfile.DOMESTIC:
            leg_raw[LegName.SMART_MONEY] = smart_money(insider_events, market_cap)

        is_stale_today = sec.id in stale_ids or sec.id in ctx.degraded_securities
        per_security_state[sec.id] = {
            "filer_profile": sec.filer_profile,
            "is_stale": is_stale_today,
            "leg_raw": leg_raw,
            "narrative_events": narrative_events,
            "revenue_growth_yoy": fundamental_inputs.revenue_growth_yoy,
            "cohort": sec.cohort,
        }

    active_ids = {
        security_id
        for security_id, state in per_security_state.items()
        if not state["is_stale"]
    }
    cohort_scopes = build_cohort_scopes(
        ctx.universe, active_ids, ctx.config["cohorts"]
    )

    def _tier_subset(source: dict[str, object], ids: tuple[str, ...]) -> dict:
        return {i: source[i] for i in ids if i in source and i in active_ids}

    for sec in ctx.universe.securities:
        state = per_security_state[sec.id]
        scope = cohort_scopes[sec.cohort]

        # Narrative premium's growth percentile, standardised within the scope.
        own_growth = state["revenue_growth_yoy"]
        growth_percentile = None
        if own_growth is not None and not state["is_stale"]:
            population = [
                per_security_state[member_id]["revenue_growth_yoy"]
                for member_id in scope.member_ids
                if per_security_state[member_id]["revenue_growth_yoy"] is not None
            ]
            growth_percentile = percentile_rank(own_growth, population)
        state["leg_raw"][LegName.NARRATIVE_PREMIUM] = narrative_premium(
            state["narrative_events"], growth_percentile
        )

        # Fundamental health: each sub-metric is standardised within the security's
        # own cohort scope (blended with parent/universe by the scope's shrinkage
        # lambdas) before the equal-weight combination, so a growth rate and an OLS
        # margin slope carry the same influence regardless of their raw units.
        health = fundamental_health(
            fundamental_inputs_by_security[sec.id],
            cohort_inputs=_tier_subset(fundamental_inputs_by_security, scope.cohort_member_ids),
            parent_inputs=_tier_subset(fundamental_inputs_by_security, scope.parent_member_ids),
            universe_inputs=_tier_subset(fundamental_inputs_by_security, scope.universe_member_ids),
            lambda_cohort=scope.lambda_cohort,
            lambda_parent=scope.lambda_parent,
        )
        state["leg_raw"][LegName.FUNDAMENTAL_HEALTH] = health.value
        state["fundamental_health_detail"] = health

        # Valuation brake's inner cross-section must be *comparable peers*, i.e. the
        # security's assigned cohort scope blended with the wider tiers — not the
        # whole investable universe, against which an EV/Sales multiple is not a
        # valuation signal. The outer composite z-score in step 12 (NORMALISE) then
        # applies the same scope to the resulting raw value.
        state["leg_raw"][LegName.VALUATION_BRAKE] = valuation_brake(
            valuation_metrics_by_security[sec.id],
            _tier_subset(valuation_metrics_by_security, scope.cohort_member_ids),
            sec.id,
            parent_metrics=_tier_subset(valuation_metrics_by_security, scope.parent_member_ids),
            universe_metrics=_tier_subset(valuation_metrics_by_security, scope.universe_member_ids),
            lambda_cohort=scope.lambda_cohort,
            lambda_parent=scope.lambda_parent,
        )

    scoring_inputs: list[SecurityScoringInput] = [
        SecurityScoringInput(
            security_id=sid,
            filer_profile=state["filer_profile"],
            is_stale=state["is_stale"],
            leg_raw=state["leg_raw"],
            not_applicable_legs=(
                frozenset({LegName.VALUATION_BRAKE}) if sid in fx_unavailable_ids else frozenset()
            ),
        )
        for sid, state in per_security_state.items()
    ]

    ctx.__dict__["_scoring_inputs"] = scoring_inputs
    ctx.__dict__["_fx_unavailable_securities"] = fx_unavailable_ids
    complete_step(manifest, "COMPUTE_RAW_LEGS", detail=f"securities={len(scoring_inputs)}")


async def step_assign_cohorts(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "ASSIGN_COHORTS")
    scoring_inputs: list[SecurityScoringInput] = ctx.__dict__.get("_scoring_inputs", [])
    active_ids = {i.security_id for i in scoring_inputs if not i.is_stale}
    scopes = build_cohort_scopes(ctx.universe, active_ids, ctx.config["cohorts"])

    cohort_scope_by_security = {}
    for sec in ctx.universe.securities:
        if sec.id in active_ids:
            cohort_scope_by_security[sec.id] = scopes[sec.cohort]
    ctx.__dict__["_cohort_scope_by_security"] = cohort_scope_by_security
    complete_step(manifest, "ASSIGN_COHORTS", detail=f"scopes={len(scopes)}")


async def step_normalise(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "NORMALISE")
    from decimal import Decimal as D

    weights_yaml = ctx.config["weights"]
    weights_by_profile = {
        FilerProfile.DOMESTIC: {LegName(k): D(v) for k, v in weights_yaml["domestic"].items()},
        FilerProfile.FPI: {LegName(k): D(v) for k, v in weights_yaml["fpi"].items() if k != "smart_money"},
    }
    scoring_inputs: list[SecurityScoringInput] = ctx.__dict__.get("_scoring_inputs", [])
    cohort_scope_by_security = ctx.__dict__.get("_cohort_scope_by_security", {})
    winsor_sigma = D(weights_yaml["winsorize_sigma"])

    results = score_universe(scoring_inputs, weights_by_profile, cohort_scope_by_security, winsor_sigma)
    ctx.__dict__["_score_results"] = results
    complete_step(manifest, "NORMALISE", detail=f"scored={len(results)}")


async def step_diff(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Per-leg z-score change since the previous *trading session* (arc42 §5.5).

    Two things this step must get right:

    * the comparison date is the previous session the market actually held, not
      ``as_of - 1 day``. Calendar yesterday is a Sunday every Monday and a
      holiday several times a year, on which no score row exists, so every leg
      delta silently vanished on exactly the days a user is most likely to look;
    * the delta is *attributed*, not merely reported. ``own_evidence_effect``
      and ``cohort_distribution_effect`` are an exact split of ``delta_z``
      (see :func:`auspex.scoring.composite.decompose_leg_delta`), and when the
      inputs for that split are missing both are written as ``null`` rather than
      dumping the whole move onto own evidence, which claimed the issuer had
      moved on days when only its peers had.
    """

    start_step(manifest, "DIFF")
    results = ctx.__dict__.get("_score_results", {})
    prior_scores = {}
    prior_date = await _previous_session_date(ctx)
    if hasattr(ctx.repos.score_repo, "for_dates"):
        prior_rows = await ctx.repos.score_repo.for_dates({prior_date})
    else:
        prior_rows = await fetch_all(ctx.repos.score_repo)
    for s in prior_rows:
        if s.as_of_date == prior_date:
            prior_scores[s.security_id] = s

    leg_changes: list[LegChange] = []
    attributed = 0
    for sec_id, res in results.items():
        prior = prior_scores.get(sec_id)
        if res.composite_result is None:
            continue
        for leg, leg_result in res.composite_result.legs.items():
            prior_leg = prior.legs.get(leg.value) if prior is not None else None
            prior_z = D_(prior_leg.z) if prior_leg is not None and prior_leg.z is not None else None
            prior_raw = D_(prior_leg.raw) if prior_leg is not None and prior_leg.raw is not None else None

            decomposition = decompose_leg_delta(
                prior_z=prior_z,
                current_z=leg_result.z,
                prior_raw=prior_raw,
                current_cross_section=leg_result.cross_section,
            )
            if decomposition.own_evidence_effect is not None:
                attributed += 1
            leg_changes.append(
                LegChange(
                    id=f"{sec_id}:{ctx.as_of_date.isoformat()}:{leg.value}",
                    security_id=sec_id,
                    as_of_date=ctx.as_of_date,
                    leg=leg,
                    prior_z=str(prior_z) if prior_z is not None else None,
                    current_z=str(leg_result.z) if leg_result.z is not None else None,
                    delta_z=str(decomposition.delta_z) if decomposition.delta_z is not None else None,
                    own_evidence_effect=(
                        str(decomposition.own_evidence_effect)
                        if decomposition.own_evidence_effect is not None
                        else None
                    ),
                    cohort_distribution_effect=(
                        str(decomposition.cohort_distribution_effect)
                        if decomposition.cohort_distribution_effect is not None
                        else None
                    ),
                    attribution_unavailable_reason=decomposition.reason_unavailable,
                )
            )
    ctx.__dict__["_leg_changes"] = leg_changes
    complete_step(
        manifest,
        "DIFF",
        detail=(
            f"leg_changes={len(leg_changes)} attributed={attributed} "
            f"prior_session={prior_date.isoformat()}"
        ),
    )


def D_(value: str) -> Decimal:
    return Decimal(value)


async def step_write_snapshot(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "WRITE_SNAPSHOT")
    from auspex.narrative.fingerprint import compute_package_fingerprint
    from auspex.scoring.composite import classify_direction

    results = ctx.__dict__.get("_score_results", {})
    config_version_id = ctx.__dict__.get("_config_version_id", "unversioned")

    prior_composites_7d: dict[str, Decimal] = {}
    # Direction compares against one trading week (5 sessions) back rather than 7
    # calendar days, which lands on a non-session whenever a holiday intervenes.
    target_date = await _prior_session_date(ctx, DIRECTION_LOOKBACK_SESSIONS, fallback_days=7)
    if hasattr(ctx.repos.score_repo, "for_dates"):
        prior_rows = await ctx.repos.score_repo.for_dates({target_date})
    else:
        prior_rows = await fetch_all(ctx.repos.score_repo)
    for s in prior_rows:
        if s.as_of_date == target_date and s.composite is not None:
            prior_composites_7d[s.security_id] = Decimal(s.composite)

    snapshots: list[ScoreSnapshot] = []
    packages_by_security: dict[str, dict] = {}
    for sec in ctx.universe.securities:
        res = results.get(sec.id)
        if res is None:
            continue
        cohort_scope = res.cohort_scope
        legs: dict[LegName, LegResult] = {}
        if res.composite_result is not None:
            for leg, leg_res in res.composite_result.legs.items():
                legs[leg] = LegResult(
                    raw=str(leg_res.raw) if leg_res.raw is not None else None,
                    z=str(leg_res.z) if leg_res.z is not None else None,
                    weight=str(leg_res.weight),
                    contribution=str(leg_res.contribution) if leg_res.contribution is not None else None,
                    computable=leg_res.computable,
                    evidence_ids=[],
                    reason_not_computable=leg_res.reason_not_computable,
                )

        composite_dec = res.composite_result.composite if res.composite_result else None
        composite_str = str(composite_dec) if composite_dec is not None else None

        direction = Direction.STABLE
        if composite_dec is not None and sec.id in prior_composites_7d:
            direction = classify_direction(composite_dec - prior_composites_7d[sec.id])

        package = {
            "security_id": sec.id,
            "as_of_date": ctx.as_of_date.isoformat(),
            "composite": composite_str,
            "percentile": res.percentile,
        }
        snapshot = ScoreSnapshot(
            id=f"{sec.id}:{ctx.as_of_date.isoformat()}",
            security_id=sec.id,
            as_of_date=ctx.as_of_date,
            config_version_id=config_version_id,
            cohort_used=cohort_scope.scope if cohort_scope else "none",
            cohort_confidence=cohort_scope.confidence if cohort_scope else CohortConfidence.LOW,
            filer_profile=sec.filer_profile,
            coverage=str(res.coverage),
            is_backfilled=False,
            legs=legs,
            composite=composite_str,
            percentile=res.percentile,
            direction=direction,
            package_fingerprint=compute_package_fingerprint(package),
            max_knowledge_date=ctx.as_of_date,
            excluded_stale=res.excluded_stale,
        )
        snapshots.append(snapshot)
        packages_by_security[sec.id] = package
        if ctx.repos.score_repo is not None:
            await ctx.repos.score_repo.upsert(snapshot)

    if ctx.repos.leg_change_repo is not None:
        for lc in ctx.__dict__.get("_leg_changes", []):
            await ctx.repos.leg_change_repo.upsert(lc)

    ctx.__dict__["_snapshots"] = snapshots
    ctx.__dict__["_packages_by_security"] = packages_by_security
    complete_step(manifest, "WRITE_SNAPSHOT", detail=f"written={len(snapshots)}")


async def _latest_fx_rate(ctx: PipelineContext) -> Decimal:
    rates = [
        rate
        for rate in await fetch_all(ctx.repos.fx_sink)
        if rate.pair == "USDCHF" and rate.session_date <= ctx.as_of_date
    ]
    if rates:
        latest = max(rates, key=lambda r: r.session_date)
        return Decimal(latest.close_rate)
    return Decimal("1")


#: How many universe members are sampled to reconstruct the trading calendar.
#: More than one because a single name can be halted, delisted or simply have a
#: gap in its history, and the calendar now decides which securities are stale
#: as well as which session a leg delta compares against. Still a small,
#: bounded number of partition-local queries rather than a scan of every price
#: partition merely to discover dates.
SESSION_CALENDAR_SAMPLE_SIZE = 5
SESSION_CALENDAR_LOOKBACK_BARS = 130


async def _session_calendar(ctx: PipelineContext) -> tuple[date, ...]:
    """Trading sessions on or before ``as_of``, derived from observed price bars.

    The union of several liquid universe members' bars defines the sessions the
    market actually held. Quarantined bars are excluded: a bar the integrity
    pass rejected is not evidence that the market was open.

    Returns an empty tuple when no bars are available; callers must then fall
    back to calendar-day arithmetic rather than silently comparing nothing.
    """

    cached = ctx.__dict__.get("_session_calendar")
    if cached is not None:
        return cached
    history_as_of = getattr(ctx.repos.price_sink, "history_as_of", None)
    try:
        if history_as_of is not None and ctx.universe.securities:
            histories = await asyncio.gather(
                *(
                    history_as_of(
                        security.id,
                        ctx.as_of_date,
                        SESSION_CALENDAR_LOOKBACK_BARS,
                    )
                    for security in ctx.universe.securities[:SESSION_CALENDAR_SAMPLE_SIZE]
                )
            )
            bars = [
                bar
                for history in histories
                for bar in history
            ]
        else:
            bars = await fetch_all(ctx.repos.price_sink)
    except Exception:  # noqa: BLE001 - calendar loss must degrade, not abort
        logger.warning(
            "trading-session calendar unavailable; using calendar-day fallback",
            exc_info=True,
        )
        bars = []
    calendar = normalise_calendar(
        bar.session_date for bar in exclude_quarantined(bars) if bar.session_date <= ctx.as_of_date
    )
    ctx.__dict__["_session_calendar"] = calendar
    return calendar


async def _prior_session_date(ctx: PipelineContext, sessions_back: int, *, fallback_days: int) -> date:
    """``sessions_back`` trading sessions before ``as_of``, or a calendar-day fallback."""

    calendar = await _session_calendar(ctx)
    if calendar:
        resolved = nth_prior_session(calendar, ctx.as_of_date, sessions_back)
        if resolved is not None:
            return resolved
    return ctx.as_of_date - timedelta(days=fallback_days)


async def _previous_session_date(ctx: PipelineContext) -> date:
    """The most recent observed trading session strictly before ``as_of``.

    Unlike ``nth_prior_session(..., 1)`` this is correct whether or not ``as_of``
    is itself a session: a run dated on a weekend compares against the Friday
    that just closed rather than skipping back over it to the Thursday.

    Falls back to calendar yesterday only when no session calendar exists at
    all, which is the one case in which nothing better can be known.
    """

    calendar = await _session_calendar(ctx)
    sessions = prior_sessions(calendar, ctx.as_of_date, 1)
    if sessions:
        return sessions[0]
    return ctx.as_of_date - timedelta(days=1)


async def _latest_price_bars(ctx: PipelineContext) -> dict[str, object]:
    """Latest non-quarantined bar per ``security_id``, at or before ``as_of``.

    One partition-local query per security in production; the full-scan branch
    exists only for the in-memory sinks used by tests and local fixtures.
    """

    cached = ctx.__dict__.get("_latest_price_bars")
    if cached is not None:
        return cached
    latest_as_of = getattr(ctx.repos.price_sink, "latest_as_of", None)
    if latest_as_of is not None:
        rows = await latest_as_of(
            ctx.as_of_date,
            [security.id for security in ctx.universe.securities],
        )
        result: dict[str, object] = {bar.security_id: bar for bar in exclude_quarantined(rows)}
    else:
        result = {}
        for bar in exclude_quarantined(await fetch_all(ctx.repos.price_sink)):
            if bar.session_date > ctx.as_of_date:
                continue
            existing = result.get(bar.security_id)
            if existing is None or bar.session_date > existing.session_date:
                result[bar.security_id] = bar
    ctx.__dict__["_latest_price_bars"] = result
    return result


async def _latest_prices_usd(ctx: PipelineContext) -> dict[str, Decimal]:
    """Latest close price per ``security_id``."""

    cached = ctx.__dict__.get("_latest_prices_usd")
    if cached is not None:
        return cached
    result = {
        security_id: Decimal(bar.close_adjusted)
        for security_id, bar in (await _latest_price_bars(ctx)).items()
    }
    ctx.__dict__["_latest_prices_usd"] = result
    return result


async def _stale_security_ids(ctx: PipelineContext) -> set[str]:
    """Securities excluded from today's cross-sections by the price-age rule.

    arc42 §5.5 "Staleness exclusion" states the rule in *trading sessions*:
    a security whose latest observed price is more than
    :data:`auspex.scoring.coverage.MAX_STALE_SESSIONS` sessions old is dropped,
    so a price that stopped updating cannot keep contributing a market cap, an
    enterprise value and a peer observation as though it were current.

    Two deliberate boundaries:

    * with no observed session calendar at all (no price history anywhere — a
      bare fixture, or a first run) the rule is unevaluable, so nothing is
      excluded on price age. Silently emptying the universe would be a far
      worse failure than scoring a day with thin prices and honest coverage;
    * once the calendar shows the market did trade, a security with no observed
      bar at all *is* stale: its price age is unbounded, which is the same
      condition the rule exists to catch, only more so.
    """

    calendar = await _session_calendar(ctx)
    if not calendar:
        return set()

    latest_bars = await _latest_price_bars(ctx)
    stale: set[str] = set()
    for security in ctx.universe.securities:
        bar = latest_bars.get(security.id)
        if bar is None:
            stale.add(security.id)
            continue
        if is_stale(
            bar.session_date,
            ctx.as_of_date,
            sessions_between(calendar, bar.session_date, ctx.as_of_date),
        ):
            stale.add(security.id)
    return stale


async def _get_portfolio_projection(ctx: PipelineContext):
    """Read the live ledger and join today's prices/FX (arc42 §5.7).

    Cached on ``ctx`` scratch state so RUN_POLICY (step 15) and
    PROJECT_PORTFOLIO (step 17) compute this exactly once per run. When no
    portfolio reader is configured (e.g. local/test execution without the
    source ledger wired), degrades to an empty snapshot with the
    context's default cash figure rather than aborting — every policy gate
    still evaluates, just against zero holdings.
    """

    if "_portfolio_projection" in ctx.__dict__:
        return ctx.__dict__["_portfolio_projection"], ctx.__dict__.get("_portfolio_snapshot")

    from auspex.portfolio.port import PortfolioSnapshot
    from auspex.portfolio.projection import project_portfolio

    ticker_by_security_id = {sec.id: sec.ticker for sec in ctx.universe.securities}
    prices_by_ticker = {
        ticker_by_security_id[sid]: price
        for sid, price in (await _latest_prices_usd(ctx)).items()
        if sid in ticker_by_security_id
    }
    fx_rate = await _latest_fx_rate(ctx)
    if ctx.providers.portfolio_reader is not None:
        snapshot = await ctx.providers.portfolio_reader.read_snapshot(
            ctx.as_of_date,
            fx_rate_to_chf=lambda currency: (
                Decimal(1)
                if currency.upper() == "CHF"
                else fx_rate
                if currency.upper() == "USD"
                else None
            ),
        )
    else:
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=ctx.cash_chf, as_of=ctx.as_of_date, lot_level=False)

    projection = project_portfolio(snapshot, prices_by_ticker, fx_rate, ctx.as_of_date)
    ctx.__dict__["_portfolio_projection"] = projection
    ctx.__dict__["_portfolio_snapshot"] = snapshot
    return projection, snapshot


def _suggested_trade_quantity(
    action: Action,
    suggested_trade_chf: Decimal,
    latest_price_usd: Decimal | None,
    fx_rate: Decimal,
    held_quantity: Decimal | None,
) -> Decimal | None:
    if (
        action not in (Action.BUY, Action.ADD, Action.TRIM, Action.SELL)
        or suggested_trade_chf <= 0
        or latest_price_usd is None
        or latest_price_usd <= 0
        or fx_rate <= 0
    ):
        return None
    if action == Action.SELL:
        return held_quantity if held_quantity is not None and held_quantity > 0 else None
    quantity = (
        suggested_trade_chf / (latest_price_usd * fx_rate)
    ).to_integral_value(rounding=ROUND_FLOOR)
    if action == Action.TRIM and held_quantity is not None:
        quantity = min(quantity, held_quantity)
    return quantity if quantity > 0 else None


def _consecutive_weakening_sessions(
    current_direction: Direction,
    prior_snapshots: list[ScoreSnapshot],
    calendar: tuple[date, ...] = (),
    as_of_date: date | None = None,
) -> int:
    """Length of the *contiguous* run of weakening sessions ending today.

    "Consecutive" has to mean consecutive. Walking an arbitrary list of prior
    snapshots and breaking only on a non-weakening direction happily welded
    together snapshots from January and March into a single long streak, which
    then tripped the sell gate on a security that had not weakened in months.
    With a session calendar we verify each step back really is the adjacent
    trading session and treat a gap as the end of the streak.
    """

    if current_direction != Direction.WEAKENING:
        return 0
    if calendar and as_of_date is not None:
        directions_by_date = {
            snapshot.as_of_date: snapshot.direction
            for snapshot in sorted(prior_snapshots, key=lambda item: item.as_of_date)
        }
        return contiguous_weakening_streak(
            current_direction,
            directions_by_date,
            calendar,
            as_of_date,
        )
    streak = 1
    for snapshot in sorted(prior_snapshots, key=lambda item: item.as_of_date, reverse=True):
        if snapshot.direction != Direction.WEAKENING:
            break
        streak += 1
    return streak


async def _active_dispositions(ctx: PipelineContext) -> dict[str, RecommendationDisposition]:
    """This user's durable dispositions, keyed by ``security_id``.

    A single partition-local read per user. Absent repository (tests, local
    fixtures) simply means no suppression, which is the safe default: the
    user sees every recommendation rather than silently losing one.
    """

    repo = ctx.repos.recommendation_disposition_repo
    if repo is None:
        return {}
    query = getattr(repo, "query", None)
    if query is None:
        return {}
    try:
        rows = await query(
            "SELECT * FROM c WHERE c.user_id = @user_id",
            [{"name": "@user_id", "value": ctx.user_id}],
            ctx.user_id,
        )
    except TypeError:
        rows = await query(
            "SELECT * FROM c WHERE c.user_id = @user_id",
            [{"name": "@user_id", "value": ctx.user_id}],
        )
    return {
        row.security_id: row
        for row in rows
        if getattr(row, "security_id", None) and getattr(row, "user_id", None) == ctx.user_id
    }


async def step_run_policy(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Deterministic gate cascade over the portfolio (arc42 §5.6, §5.7).

    Reads the current portfolio state through the read-only
    :class:`~auspex.portfolio.port.PortfolioPort`. The nightly job is read-only
    against the live ledger; API mutation is separately scoped. Every gate depends only on quantity and cash, so this
    works fully even when richer fields (cost basis, FX-at-open) are absent.
    """

    from auspex.policy.cost import estimate_total_cost_usd
    from auspex.policy.gates import PolicyContext as GatePolicyContext
    from auspex.policy.signature import compute_decision_signature, evidence_fingerprint
    from auspex.policy.target_weight import target_weight_pct

    start_step(manifest, "RUN_POLICY")
    user_settings = None
    if ctx.repos.user_settings_repo is not None:
        get_settings = getattr(ctx.repos.user_settings_repo, "get", None)
        if get_settings is not None:
            user_settings = await get_settings(ctx.user_id, ctx.user_id)
    thresholds = load_policy_thresholds(
        ctx.config["policy"],
        risk_profile=(
            user_settings.risk_profile.value
            if user_settings is not None
            else "MODERATE"
        ),
        cash_reserve_chf=(
            user_settings.cash_reserve_chf
            if user_settings is not None
            else None
        ),
    )
    fees_config = ctx.config["fees"]

    projection, snapshot = await _get_portfolio_projection(ctx)
    positions_by_ticker = {p.ticker: p for p in projection.positions}
    ticker_by_security_id = {sec.id: sec.ticker for sec in ctx.universe.securities}

    total_portfolio_value_chf = projection.total_value_chf
    cash_chf = snapshot.cash_chf if snapshot is not None else ctx.cash_chf
    fx_rate = await _latest_fx_rate(ctx)
    latest_prices_usd = await _latest_prices_usd(ctx)

    results = ctx.__dict__.get("_score_results", {})
    snapshots_by_security = {
        snapshot.security_id: snapshot
        for snapshot in ctx.__dict__.get("_snapshots", [])
    }
    weakening_lookback_days = max(
        14,
        thresholds.sell_min_consecutive_weakening_sessions * 4,
    )
    session_calendar = await _session_calendar(ctx)
    if session_calendar:
        # Read back the required number of *sessions*; a fixed calendar-day
        # window silently truncates the streak whenever holidays intervene.
        weakening_dates = set(
            prior_sessions(
                session_calendar,
                ctx.as_of_date,
                max(weakening_lookback_days, thresholds.sell_min_consecutive_weakening_sessions + 1),
            )
        )
    else:
        weakening_dates = {
            ctx.as_of_date - timedelta(days=offset)
            for offset in range(1, weakening_lookback_days + 1)
        }
    if hasattr(ctx.repos.score_repo, "for_dates"):
        prior_score_rows = await ctx.repos.score_repo.for_dates(weakening_dates)
    else:
        prior_score_rows = [
            snapshot
            for snapshot in await fetch_all(ctx.repos.score_repo)
            if snapshot.as_of_date in weakening_dates
        ]
    prior_scores_by_security: dict[str, list[ScoreSnapshot]] = {}
    for snapshot in prior_score_rows:
        prior_scores_by_security.setdefault(snapshot.security_id, []).append(snapshot)

    actions: list[Action] = []
    eligible_but_no_cash_count = 0
    recommendations = []
    recommendation_context: dict[str, dict] = {}
    suppressed_count = 0
    dispositions = await _active_dispositions(ctx)
    evaluated_at = utc_now()

    for sec in ctx.universe.securities:
        res = results.get(sec.id)
        if res is None or res.composite_result is None:
            continue

        position = positions_by_ticker.get(ticker_by_security_id.get(sec.id, sec.ticker))
        held = position is not None and position.quantity > 0
        current_weight_pct = (
            (position.weight * 100) if (position is not None and position.weight is not None) else Decimal(0)
        )
        target_pct = target_weight_pct(
            res.percentile,
            max_pct=thresholds.target_weight_max_pct,
            floor_pct=thresholds.target_weight_floor_pct,
        )

        gap_chf = max((target_pct - current_weight_pct) / 100 * total_portfolio_value_chf, Decimal(0))
        individually_available_cash_chf = max(
            cash_chf - thresholds.buy_min_cash_after_trade_chf,
            Decimal(0),
        )
        trade_notional_chf = min(
            gap_chf,
            individually_available_cash_chf,
        )
        resulting_weight_pct = current_weight_pct + (
            trade_notional_chf / total_portfolio_value_chf * 100 if total_portfolio_value_chf > 0 else Decimal(0)
        )
        cash_after_trade_chf = cash_chf - trade_notional_chf

        trade_notional_usd = trade_notional_chf / fx_rate if fx_rate > 0 else Decimal(0)
        estimated_cost_usd = (
            estimate_total_cost_usd(trade_notional_usd, fees_config) if trade_notional_usd > 0 else Decimal(0)
        )
        estimated_cost_chf = estimated_cost_usd * fx_rate

        thesis_z = res.composite_result.legs.get(LegName.THESIS_LINKAGE)
        valuation_z = res.composite_result.legs.get(LegName.VALUATION_BRAKE)
        current_direction = (
            snapshots_by_security[sec.id].direction
            if sec.id in snapshots_by_security
            else Direction.STABLE
        )

        gate_ctx = GatePolicyContext(
            security_id=sec.id,
            held=held,
            percentile=res.percentile,
            coverage=res.coverage,
            cohort_confidence=res.cohort_scope.confidence if res.cohort_scope else CohortConfidence.LOW,
            valuation_brake_z=valuation_z.z if valuation_z else None,
            thesis_linkage_z=thesis_z.z if thesis_z else None,
            direction=current_direction,
            consecutive_weakening_sessions=_consecutive_weakening_sessions(
                current_direction,
                prior_scores_by_security.get(sec.id, []),
                session_calendar,
                ctx.as_of_date,
            ),
            current_weight_pct=current_weight_pct,
            target_weight_pct=target_pct,
            resulting_weight_pct=resulting_weight_pct,
            cash_after_trade_chf=cash_after_trade_chf,
            estimated_cost_chf=estimated_cost_chf,
            trade_notional_chf=trade_notional_chf,
        )

        from auspex.policy.engine import evaluate_action

        action, trace = evaluate_action(gate_ctx, thresholds)
        suggested_trade_chf = Decimal(0)
        if action in (Action.BUY, Action.ADD):
            suggested_trade_chf = trade_notional_chf
        elif action in (Action.TRIM, Action.SELL):
            desired_weight_pct = Decimal(0) if action == Action.SELL else target_pct
            suggested_trade_chf = max(
                (current_weight_pct - desired_weight_pct)
                / Decimal(100)
                * total_portfolio_value_chf,
                Decimal(0),
            )
        suggested_quantity = None
        latest_price_usd = latest_prices_usd.get(sec.id)
        quantity = _suggested_trade_quantity(
            action,
            suggested_trade_chf,
            latest_price_usd,
            fx_rate,
            position.quantity if position is not None else None,
        )
        if quantity is not None and latest_price_usd is not None:
            suggested_quantity = str(quantity)
            suggested_trade_chf = quantity * latest_price_usd * fx_rate

        if action in (Action.HOLD_NO_ACTION,) and not held and trade_notional_chf < thresholds.buy_min_trade_chf:
            eligible_but_no_cash_count += 1

        from auspex.models.policy import CostOutcomeOverlay, Recommendation

        action_trade_usd = (
            suggested_trade_chf / fx_rate if fx_rate > 0 else Decimal(0)
        )
        action_cost_usd = (
            estimate_total_cost_usd(action_trade_usd, fees_config)
            if action_trade_usd > 0
            else Decimal(0)
        )
        action_cost_chf = action_cost_usd * fx_rate
        cost_overlay = None
        if action in (Action.TRIM, Action.SELL):
            position_value_chf = (
                position.market_value_chf
                if position is not None and position.market_value_chf is not None
                else Decimal(0)
            )
            sold_fraction = (
                min(suggested_trade_chf / position_value_chf, Decimal(1))
                if position_value_chf > 0
                else Decimal(0)
            )
            cost_overlay = CostOutcomeOverlay(
                realised_gain_chf=(
                    str(position.unrealised_chf * sold_fraction)
                    if position is not None and position.unrealised_chf is not None
                    else None
                ),
                fx_effect_chf=(
                    str(position.fx_effect_chf * sold_fraction)
                    if position is not None and position.fx_effect_chf is not None
                    else None
                ),
                holding_period_days=position.holding_period_days if position else None,
                estimated_cost_chf=str(action_cost_chf),
                cost_as_pct_of_position=(
                    str(action_cost_chf / suggested_trade_chf * Decimal(100))
                    if suggested_trade_chf > 0
                    else None
                ),
            )

        rec = Recommendation(
            id=f"{ctx.user_id}:{sec.id}:{ctx.as_of_date.isoformat()}",
            user_id=ctx.user_id,
            security_id=sec.id,
            as_of_date=ctx.as_of_date,
            action=action,
            target_weight_pct=str(target_pct),
            current_weight_pct=str(current_weight_pct),
            suggested_trade_chf=str(suggested_trade_chf),
            suggested_quantity=suggested_quantity,
            gate_trace=trace,
            cost_overlay=cost_overlay,
            config_version_id=ctx.__dict__.get("_config_version_id", "unversioned"),
        )
        recommendations.append(rec)
        recommendation_context[sec.id] = {
            "security": sec,
            "result": res,
            "gate_context": gate_ctx,
            "position": position,
            "latest_price_usd": latest_price_usd,
            "preliminary_action": action,
            "estimated_cost_chf": action_cost_chf,
        }

    from auspex.models.user_settings import InvestmentHorizon, InvestmentObjective
    from auspex.policy.allocation import (
        AllocationCandidate,
        AllocationConstraints,
        allocate_candidates,
        allocation_gate_trace,
        preference_constraints,
    )

    market_risk_estimates = ctx.__dict__.get("_market_risk_estimates", {})
    correlation_group_by_security = ctx.__dict__.get(
        "_correlation_groups",
        {},
    )
    cohort_by_ticker = {
        security.ticker: security.cohort
        for security in ctx.universe.securities
    }
    security_by_ticker = {
        security.ticker: security.id
        for security in ctx.universe.securities
    }
    current_cohort_weights_pct: dict[str, Decimal] = {}
    current_correlated_group_weights_pct: dict[str, Decimal] = {}
    for portfolio_position in projection.positions:
        if portfolio_position.weight is None:
            continue
        weight_pct = portfolio_position.weight * Decimal(100)
        cohort = cohort_by_ticker.get(portfolio_position.ticker)
        if cohort is not None:
            current_cohort_weights_pct[cohort] = (
                current_cohort_weights_pct.get(cohort, Decimal(0))
                + weight_pct
            )
        security_id = security_by_ticker.get(portfolio_position.ticker)
        correlation_group = (
            correlation_group_by_security.get(security_id)
            if security_id is not None
            else None
        )
        if correlation_group is not None:
            current_correlated_group_weights_pct[correlation_group] = (
                current_correlated_group_weights_pct.get(
                    correlation_group,
                    Decimal(0),
                )
                + weight_pct
            )

    def candidates(*, risk_aware: bool) -> list[AllocationCandidate]:
        rows = []
        for recommendation in recommendations:
            metadata = recommendation_context[recommendation.security_id]
            risk = market_risk_estimates.get(recommendation.security_id)
            rows.append(
                AllocationCandidate(
                    security_id=recommendation.security_id,
                    ticker=metadata["security"].ticker,
                    cohort=metadata["security"].cohort,
                    correlation_group=correlation_group_by_security.get(
                        recommendation.security_id
                    ),
                    action=recommendation.action,
                    percentile=metadata["result"].percentile or 0,
                    direction=metadata["gate_context"].direction,
                    requested_trade_chf=Decimal(
                        recommendation.suggested_trade_chf or "0"
                    ),
                    current_weight_pct=Decimal(
                        recommendation.current_weight_pct or "0"
                    ),
                    estimated_cost_chf=metadata["estimated_cost_chf"],
                    volatility_60d=(
                        risk.volatility_60d
                        if risk_aware and risk is not None
                        else None
                    ),
                    average_daily_value_chf=(
                        risk.average_daily_value_chf
                        if risk_aware and risk is not None
                        else None
                    ),
                )
            )
        return rows

    shared_cash_constraints = AllocationConstraints(
        cash_chf=cash_chf,
        cash_reserve_chf=thresholds.buy_min_cash_after_trade_chf,
        total_value_chf=total_portfolio_value_chf,
        max_position_pct=Decimal("100"),
        max_cohort_pct=Decimal("100"),
        max_correlated_group_pct=Decimal("100"),
        max_buy_turnover_pct=Decimal("100"),
        max_daily_volume_participation=Decimal("1"),
        min_trade_chf=thresholds.buy_min_trade_chf,
        # The promoted v4.2 allocator fixes joint cash feasibility only. Full
        # liquidity/volatility/correlation limits remain in the risk-aware
        # shadow arm below until the registered promotion gate passes.
    )
    production_allocations = allocate_candidates(
        candidates(risk_aware=False),
        shared_cash_constraints,
    )
    settings_horizon = (
        user_settings.investment_horizon
        if user_settings is not None
        else InvestmentHorizon.OVER_SEVEN_YEARS
    )
    settings_objective = (
        user_settings.investment_objective
        if user_settings is not None
        else InvestmentObjective.CAPITAL_GROWTH
    )
    allocation_config = ctx.config["policy"].get("allocation", {})
    shadow_constraints = preference_constraints(
        horizon=settings_horizon,
        objective=settings_objective,
        policy_max_position_pct=thresholds.target_weight_max_pct,
        cash_chf=cash_chf,
        cash_reserve_chf=thresholds.buy_min_cash_after_trade_chf,
        total_value_chf=total_portfolio_value_chf,
        min_trade_chf=thresholds.buy_min_trade_chf,
        current_cohort_weights_pct=current_cohort_weights_pct,
        current_correlated_group_weights_pct=(
            current_correlated_group_weights_pct
        ),
        max_daily_volume_participation=Decimal(
            str(
                allocation_config.get(
                    "max_daily_volume_participation",
                    "0.01",
                )
            )
        ),
        allocation_config=allocation_config,
    )
    shadow_allocations = allocate_candidates(
        candidates(risk_aware=True),
        shadow_constraints,
    )

    for recommendation in recommendations:
        metadata = recommendation_context[recommendation.security_id]
        decision = production_allocations[recommendation.security_id]
        shadow = shadow_allocations[recommendation.security_id]
        recommendation.allocation_mode = "JOINT_CASH"
        recommendation.shadow_suggested_trade_chf = str(
            shadow.allocated_trade_chf
        )
        recommendation.allocation_trace = allocation_gate_trace(decision)
        recommendation.gate_trace.extend(recommendation.allocation_trace)
        preliminary_action = metadata["preliminary_action"]
        if preliminary_action in {Action.BUY, Action.ADD}:
            if decision.allocated_trade_chf <= 0:
                recommendation.action = Action.HOLD_NO_ACTION
                recommendation.suggested_trade_chf = "0"
                recommendation.suggested_quantity = None
                eligible_but_no_cash_count += 1
            else:
                quantity = _suggested_trade_quantity(
                    preliminary_action,
                    decision.allocated_trade_chf,
                    metadata["latest_price_usd"],
                    fx_rate,
                    (
                        metadata["position"].quantity
                        if metadata["position"] is not None
                        else None
                    ),
                )
                if quantity is None:
                    recommendation.action = Action.HOLD_NO_ACTION
                    recommendation.suggested_trade_chf = "0"
                    recommendation.suggested_quantity = None
                    eligible_but_no_cash_count += 1
                else:
                    recommendation.suggested_quantity = str(quantity)
                    recommendation.suggested_trade_chf = str(
                        quantity
                        * metadata["latest_price_usd"]
                        * fx_rate
                    )

        recommendation.decision_signature = compute_decision_signature(
            action=recommendation.action,
            security_id=recommendation.security_id,
            suggested_quantity=recommendation.suggested_quantity,
            suggested_trade_chf=recommendation.suggested_trade_chf,
            target_weight_pct=recommendation.target_weight_pct,
            gate_trace=recommendation.gate_trace,
            evidence=evidence_fingerprint(
                percentile=metadata["result"].percentile,
                cohort_confidence=metadata["gate_context"].cohort_confidence,
                direction=metadata["gate_context"].direction,
                coverage=metadata["result"].coverage,
            ),
        )
        disposition = dispositions.get(recommendation.security_id)
        if disposition is not None and disposition.suppresses(
            recommendation.decision_signature,
            now=evaluated_at,
        ):
            recommendation.suppressed = True
            recommendation.disposition = disposition.disposition
            recommendation.suppression_reason = (
                f"{disposition.disposition.value} on an identical decision signature"
            )
            suppressed_count += 1
        actions.append(recommendation.action)
        if ctx.repos.recommendation_repo is not None:
            await ctx.repos.recommendation_repo.upsert(recommendation)

    ctx.__dict__["_actions"] = actions
    ctx.__dict__["_eligible_but_no_cash_count"] = eligible_but_no_cash_count
    complete_step(
        manifest,
        "RUN_POLICY",
        detail=f"recommendations={len(recommendations)} suppressed={suppressed_count}",
    )


async def step_assert(ctx: PipelineContext, manifest: RunManifest) -> None:
    start_step(manifest, "ASSERT")
    actions = ctx.__dict__.get("_actions", [])
    scored_count = len(ctx.__dict__.get("_snapshots", []))
    eligible_but_no_cash_count = ctx.__dict__.get("_eligible_but_no_cash_count", 0)

    violations = run_post_run_assertions(
        actions=actions,
        scored_security_count=scored_count,
        eligible_but_no_cash_count=eligible_but_no_cash_count,
        policy_config=ctx.config["policy"],
    )
    ctx.__dict__["_assertion_violations"] = violations
    if violations:
        detail = "; ".join(f"{v.name}: {v.detail}" for v in violations)
        complete_step(manifest, "ASSERT", detail=detail, degraded=True)
    else:
        complete_step(manifest, "ASSERT", detail="all assertions passed")


async def step_project_portfolio(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Read the external ledger, join prices + FX, write Auspex's own
    `portfolio_projection` (arc42 §5.7 "Daily projection", §6.1 step 17).

    Read-only within the nightly pipeline: this step never writes back, only
    to Auspex's own `portfolio_projection` container.
    """

    start_step(manifest, "PROJECT_PORTFOLIO")
    if ctx.repos.portfolio_projection_repo is None:
        skip_step(manifest, "PROJECT_PORTFOLIO", detail="no portfolio_projection repository configured")
        return

    projection, _ = await _get_portfolio_projection(ctx)

    from auspex.models.common import utc_now
    from auspex.models.portfolio import PortfolioProjection, PositionProjectionRow

    read_at = utc_now()
    rows = [
        PositionProjectionRow(
            ticker=p.ticker,
            quantity=str(p.quantity),
            weight=str(p.weight) if p.weight is not None else None,
            market_value_usd=str(p.market_value_usd) if p.market_value_usd is not None else None,
            market_value_chf=str(p.market_value_chf) if p.market_value_chf is not None else None,
            cost_basis_usd=str(p.cost_basis_usd) if p.cost_basis_usd is not None else None,
            cost_basis_chf=str(p.cost_basis_chf) if p.cost_basis_chf is not None else None,
            unrealised_usd=str(p.unrealised_usd) if p.unrealised_usd is not None else None,
            unrealised_chf=str(p.unrealised_chf) if p.unrealised_chf is not None else None,
            fx_effect_chf=str(p.fx_effect_chf) if p.fx_effect_chf is not None else None,
            holding_period_days=p.holding_period_days,
            source_ledger_read_at=read_at,
            degraded_fields=p.degraded_fields,
        )
        for p in projection.positions
    ]
    doc = PortfolioProjection(
        id=f"{ctx.user_id}:{ctx.as_of_date.isoformat()}",
        user_id=ctx.user_id,
        as_of_date=ctx.as_of_date,
        lot_level=projection.lot_level,
        total_value_chf=str(projection.total_value_chf),
        invested_chf=str(projection.invested_chf),
        total_gain_chf=str(projection.total_gain_chf),
        cash_chf=str(projection.cash_chf),
        dividends_chf=str(projection.dividends_chf),
        expenses_chf=str(projection.expenses_chf),
        withdrawals_chf=str(projection.withdrawals_chf),
        positions=rows,
        degraded_fields=projection.degraded_fields,
    )
    await ctx.repos.portfolio_projection_repo.upsert(doc)
    complete_step(manifest, "PROJECT_PORTFOLIO", detail=f"positions={len(rows)}")


async def step_narrate(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Per-security daily narrative (arc42 §5.9): grounded strictly in the
    deterministic package WRITE_SNAPSHOT (step 14) just persisted, this
    run's leg changes (step 13 DIFF), and today's Channel B evidence bundle
    for that security. Cached on ``package_fingerprint + model_version +
    prompt_version`` (arc42 §5.9), so replaying an unchanged day is a pure
    cache hit — never a second LLM call. The generated text is also written
    back onto that security's just-persisted :class:`ScoreSnapshot`
    (idempotent upsert on the same ``id``), which is where API readers
    expect a narrative to live (``ScoreSnapshot.narrative``).

    This is also where a Channel B failure finally surfaces. Channel B feeds
    narratives and digests, never one of the six legs, so ``EXTRACT_CHANNEL_B``
    records its failures on ``ctx.explanation_degraded_securities`` rather than
    excluding those securities from scoring. Their score, percentile and
    recommendation are unaffected and correct; what is thinner is the *evidence
    this step had to explain them with*. Counting them here marks the run
    degraded for the right reason — a weaker explanation — instead of silently
    serving a narrative built on less evidence than the reader assumes.
    """

    start_step(manifest, "NARRATE")
    if ctx.providers.openai_client is None or ctx.repos.narrative_sink is None:
        skip_step(manifest, "NARRATE", detail="no LLM/sink configured")
        return

    settings = get_settings()
    generator = NarrativeGenerator(
        openai_client=ctx.providers.openai_client,
        deployment=settings.aoai_deployment_narrative,
        system_prompt=load_prompt(NarrativeGenerator.prompt_version),
        model_version=settings.aoai_deployment_narrative,
        sink=ctx.repos.narrative_sink,
    )

    packages_by_security: dict[str, dict] = ctx.__dict__.get("_packages_by_security", {})
    snapshots_by_security = {s.security_id: s for s in ctx.__dict__.get("_snapshots", [])}

    leg_changes_by_security: dict[str, list[dict]] = {}
    for lc in ctx.__dict__.get("_leg_changes", []):
        leg_changes_by_security.setdefault(lc.security_id, []).append(lc.model_dump(mode="json"))

    all_digests = await fetch_all(ctx.repos.channel_b_sink) if ctx.repos.channel_b_sink is not None else []
    digests_by_security: dict[str, list] = {}
    for digest in all_digests:
        digests_by_security.setdefault(digest.security_id, []).append(digest)

    generated = 0
    narrated_with_degraded_evidence = 0
    for sec in ctx.universe.securities:
        package = packages_by_security.get(sec.id)
        if package is None:
            continue  # nothing scored for this security this run — nothing to narrate

        new_doc_ids = set(ctx.new_document_ids_by_security.get(sec.id, []))
        todays_digests = [d for d in digests_by_security.get(sec.id, []) if d.document_id in new_doc_ids]
        comparative = next((d.comparative for d in todays_digests if d.comparative is not None), None)

        narrative_text = await generator.generate(
            package=package,
            leg_changes=leg_changes_by_security.get(sec.id, []),
            digests=todays_digests,
            comparative=comparative,
        )
        generated += 1
        if sec.id in ctx.explanation_degraded_securities:
            narrated_with_degraded_evidence += 1

        snapshot = snapshots_by_security.get(sec.id)
        if snapshot is not None and ctx.repos.score_repo is not None:
            snapshot.narrative = narrative_text
            snapshot.narrative_model_version = settings.aoai_deployment_narrative
            await ctx.repos.score_repo.upsert(snapshot)

    detail = f"generated={generated}"
    if narrated_with_degraded_evidence:
        detail += f"; explanation_evidence_degraded={narrated_with_degraded_evidence}"
    complete_step(
        manifest,
        "NARRATE",
        detail=detail,
        degraded=narrated_with_degraded_evidence > 0,
    )


async def step_validate(ctx: PipelineContext, manifest: RunManifest) -> None:
    """Final sanity gate before END_RUN (arc42 §6.1 step 19): every
    expected write actually landed and is internally consistent. A failure
    here degrades the run (visibly flagged, still published — arc42 §5.6),
    it never rolls anything back.
    """

    start_step(manifest, "VALIDATE")
    snapshots = ctx.__dict__.get("_snapshots", [])
    issues: list[str] = []

    if not snapshots:
        issues.append("no snapshots written")

    fx_rows = await fetch_all(ctx.repos.fx_sink)
    if not fx_rows:
        issues.append("no FX rate present (ledger revaluation deferred)")

    missing_fingerprint = [s.security_id for s in snapshots if not s.package_fingerprint]
    if missing_fingerprint:
        issues.append(f"{len(missing_fingerprint)} snapshot(s) missing package_fingerprint")

    if snapshots and ctx.repos.recommendation_repo is not None:
        # Scoped to this run's representative user: with several users the
        # same day legitimately holds N x snapshots recommendations, so an
        # unscoped count would report a phantom mismatch every night.
        recommendations_today = [
            r
            for r in await fetch_all(ctx.repos.recommendation_repo)
            if r.as_of_date == ctx.as_of_date and r.user_id == ctx.user_id
        ]
        score_results = ctx.__dict__.get("_score_results", {})
        if score_results:
            expected_security_ids = {
                security_id
                for security_id, result in score_results.items()
                if result.composite_result is not None
            }
        else:
            expected_security_ids = {
                snapshot.security_id
                for snapshot in snapshots
                if not snapshot.excluded_stale and snapshot.composite is not None
            }
        recommendation_security_ids = {
            recommendation.security_id
            for recommendation in recommendations_today
        }
        missing = sorted(expected_security_ids - recommendation_security_ids)
        unexpected = sorted(recommendation_security_ids - expected_security_ids)
        if missing or unexpected:
            issues.append(
                "recommendation security set does not match policy-evaluable "
                f"score set (missing={len(missing)}, unexpected={len(unexpected)})"
            )

    if issues:
        complete_step(manifest, "VALIDATE", detail="; ".join(issues), degraded=True)
    else:
        complete_step(
            manifest, "VALIDATE", detail="expected counts present, FX present, recommendations reconciled"
        )


async def step_end_run(ctx: PipelineContext, manifest: RunManifest) -> None:
    from auspex.models.enums import RunStatus
    from auspex.pipeline.manifest import finalize

    start_step(manifest, "END_RUN")
    any_degraded = any(cp.degraded for cp in manifest.steps) or bool(ctx.__dict__.get("_assertion_violations"))
    status = RunStatus.DEGRADED if any_degraded else RunStatus.SUCCESS
    manifest.scored_security_count = len(ctx.__dict__.get("_snapshots", []))
    actions = ctx.__dict__.get("_actions", [])
    hold_insufficient = sum(1 for a in actions if a == Action.HOLD_INSUFFICIENT_DATA)
    manifest.hold_insufficient_data_fraction = (
        str(Decimal(hold_insufficient) / Decimal(len(actions))) if actions else "0"
    )
    finalize(manifest, status=status, watermarks_committed=True)
    complete_step(manifest, "END_RUN", detail=f"status={status.value}")
