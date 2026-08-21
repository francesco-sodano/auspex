"""Cold-start bootstrap (arc42 §6.3) — a single automated job, run once.

Two windows, not one (arc42 §6.3 "Two windows, not one"): 36 months of raw
prices/XBRL/Form 4 (warm-up for trailing metrics), 18 months of Channel A/B
extraction and scored days (no leg looks back beyond 180 days, so extracting
a 2023 8-K is pure waste).

```
 1. Load config/universe.yaml; resolve CIK via EDGAR company_tickers.json
 2. Verify filer_profile against EDGAR formType history — fail loudly on mismatch
 3. Fetch submissions.zip + companyfacts.zip (bulk) — full filing index and XBRL history
 4. Backfill 36 months of daily prices, corporate actions, USD/CHF
 5. Download filing documents for the 18-month extraction window
 6. Download and parse ALL Form 4 XML across 36 months — deterministic, no LLM
 7. Run Channel A + B extraction over the 18-month document set
 8. Backfill news for whatever window the provider licence permits (typically <=12 months)
 9. Replay scoring day by day across 18 months, chronologically, filtering every
    source on knowledge_date <= as_of_date. Write scores with is_backfilled=true
10. Compute performance metrics over the replayed history
11. Bind and validate the existing portfolio ledger (§5.7): resolve mapping, assert
    required fields, assert every ticker maps to the universe, determine lot_level,
    log the mapped sample document and binding summary, and REQUIRE explicit
    non-interactive confirmation (AUSPEX_CONFIRM_PORTFOLIO_BINDING=true) before
    proceeding — never defaults to confirmed
12. Validate: >=85 securities scored on >=370 of the last 378 sessions
```

Runtime budget (arc42 §6.3 "Runtime budget"): approximately 2.5-5 hours,
dominated entirely by Channel A + B extraction (1.5-4 hrs, bounded by the
Azure OpenAI tokens-per-minute quota) — everything else (bulk zips, prices/FX,
Form 4 parsing, score replay, performance metrics) is single-digit minutes.

Step 3 (bulk archive fetch) streams ``submissions.zip``/``companyfacts.zip``
via HTTP Range requests (:mod:`auspex.providers.edgar_bulk`) rather than
downloading either multi-GB archive in full to local ephemeral storage —
only the 104 universe securities' CIK entries are ever transferred, and only
those are persisted, as raw artefacts, via ``blob_sink``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from auspex.collectors.base import watermark_key
from auspex.collectors.filing_collector import INTERESTING_FORMS
from auspex.collectors.fx_collector import COLLECTOR_NAME as _FX_COLLECTOR_NAME
from auspex.config.loader import Universe
from auspex.marketdata.quarantine import exclude_quarantined
from auspex.models.enums import Action, FilerProfile, LegName
from auspex.models.performance import PerformanceMetric
from auspex.performance.engine import (
    HORIZONS,
    DateCrossSection,
    compute_composite_ic_metrics,
    compute_detailed_metrics,
    compute_disposition_outcome_metric,
    compute_leg_ic_metrics,
    compute_suggestion_hit_rate_metric,
)
from auspex.performance.hit_rate import DispositionOutcome, SuggestionOutcome
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.repo_access import fetch_all
from auspex.pipeline.steps import (
    step_assign_cohorts,
    step_collect_filings,
    step_collect_fundamentals,
    step_collect_fx,
    step_collect_insiders,
    step_collect_news,
    step_collect_prices,
    step_compute_raw_legs,
    step_diff,
    step_extract_channel_a,
    step_extract_channel_b,
    step_normalise,
    step_write_snapshot,
)
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.validation import BindingValidationResult, validate_portfolio_binding
from auspex.providers.edgar import EdgarClient
from auspex.providers.edgar_bulk import BulkEdgarSource

logger = logging.getLogger("auspex.bootstrap")

# arc42 §6.3 "Two windows, not one"
RAW_BACKFILL_MONTHS = 36
EXTRACTION_BACKFILL_MONTHS = 18

# arc42 A-01 / §6.3 step 12: ~378 sessions in 18 months, >=370 must be scored
MIN_SCORED_SECURITIES = 85
MIN_SESSIONS_SCORED = 370
TOTAL_RECENT_SESSIONS = 378

# arc42 §6.3 "Runtime budget": approximately 2.5-5 hours end to end
RUNTIME_BUDGET_MIN_HOURS = 2.5
RUNTIME_BUDGET_MAX_HOURS = 5.0


class _AsOfPriceSink:
    """Wraps a real ``PriceSink`` so :func:`auspex.pipeline.steps._latest_prices_usd`
    (which reads the *latest* stored bar per security with no date bound of
    its own — a point-in-time gap in ``pipeline.steps`` that is out of this
    module's ownership to fix) only ever sees bars up to a fixed ``as_of``
    date during :meth:`BootstrapRunner.replay_scoring`'s day-by-day replay.

    Writes pass straight through untouched; the read path is filtered by
    delegating to :func:`auspex.pipeline.repo_access.fetch_all`, which
    already knows how to read either an in-memory fixture or a production
    Cosmos-backed sink — this wrapper only adds the ``as_of`` date bound.
    """

    def __init__(self, delegate, as_of: date, security_ids: list[str]) -> None:
        self._delegate = delegate
        self._as_of = as_of
        self._security_ids = security_ids

    async def upsert_price_bar(self, bar) -> None:
        await self._delegate.upsert_price_bar(bar)

    async def all(self) -> list:
        if hasattr(self._delegate, "latest_as_of"):
            return await self._delegate.latest_as_of(self._as_of, self._security_ids)
        bars = await fetch_all(self._delegate)
        return [b for b in bars if b.session_date <= self._as_of]


class _ReplayScoreRepository:
    def __init__(self, delegate, existing: list) -> None:
        self._delegate = delegate
        self._by_date: dict[date, dict[str, object]] = {}
        for item in existing:
            self._by_date.setdefault(item.as_of_date, {})[item.id] = item

    async def all(self) -> list:
        return [
            item
            for rows in self._by_date.values()
            for item in rows.values()
        ]

    async def for_dates(self, dates: set[date]) -> list:
        return [
            item
            for as_of_date in dates
            for item in self._by_date.get(as_of_date, {}).values()
        ]

    async def upsert(self, item) -> None:
        self._by_date.setdefault(item.as_of_date, {})[item.id] = item
        await self._delegate.upsert(item)

    def discard_before(self, cutoff: date) -> None:
        self._by_date = {
            as_of_date: rows
            for as_of_date, rows in self._by_date.items()
            if as_of_date >= cutoff
        }


def _forward_return_usd(
    bars_by_security: dict,
    security_id: str,
    as_of: date,
    horizon_days: int,
    dates_by_security: dict[str, list[date]] | None = None,
) -> Decimal | None:
    """Return the ``horizon_days``-trading-session-ahead USD return for
    ``security_id`` from ``as_of``, or ``None`` if either endpoint bar is
    missing (arc42 §5.8: forward returns are computed over full price
    history, unlike step 9's as-of-bounded proxy for scoring itself)."""

    bars = bars_by_security.get(security_id)
    if not bars:
        return None

    dates = (
        dates_by_security[security_id]
        if dates_by_security is not None
        else [bar.session_date for bar in bars]
    )
    start_idx = bisect_left(dates, as_of)
    if start_idx >= len(bars):
        return None

    end_idx = start_idx + horizon_days
    if end_idx >= len(bars):
        return None

    start_close = Decimal(bars[start_idx].close_adjusted)
    end_close = Decimal(bars[end_idx].close_adjusted)
    if start_close == 0:
        return None
    return (end_close - start_close) / start_close


def _trailing_return_usd(
    bars_by_security: dict,
    security_id: str,
    as_of: date,
    window_sessions: int,
    dates_by_security: dict[str, list[date]] | None = None,
) -> Decimal | None:
    bars = bars_by_security.get(security_id)
    if not bars or window_sessions <= 0:
        return None
    dates = (
        dates_by_security[security_id]
        if dates_by_security is not None
        else [bar.session_date for bar in bars]
    )
    end_idx = bisect_right(dates, as_of) - 1
    start_idx = end_idx - window_sessions
    if end_idx < 0 or start_idx < 0:
        return None
    start_close = Decimal(bars[start_idx].close_adjusted)
    end_close = Decimal(bars[end_idx].close_adjusted)
    if start_close == 0:
        return None
    return (end_close - start_close) / start_close


class RawArtefactSink(Protocol):
    """The one blob-storage operation step 3 needs — matches
    :class:`~auspex.persistence.blob_client.BlobContext` and
    :class:`~auspex.persistence.memory.InMemoryBlobSink`."""

    async def upload_document_blob(self, security_id: str, document_id: str, ext: str, content: bytes | str) -> str: ...


class PortfolioBindingNotConfirmedError(RuntimeError):
    """Step 11's portfolio binding was not explicitly confirmed for this run.

    Bootstrap runs unattended for 2.5-5 hours against the real, owner-owned
    source ledger — it must never silently proceed past an unreviewed
    binding just because ``bind_and_validate_portfolio`` happened to run
    non-interactively. An operator must read the mapped sample document and
    binding summary logged just before this is raised, then explicitly set
    ``AUSPEX_CONFIRM_PORTFOLIO_BINDING=true`` (``Settings.confirm_portfolio_binding``)
    for *this* run before re-running bootstrap. There is no default-true
    path anywhere in this flow.
    """


@dataclass(frozen=True)
class BulkArchiveResult:
    """Per-security raw documents extracted from the bulk archives, keyed by
    ``security_id`` — never the full archive, never a local file."""

    submissions_by_security: dict[str, dict]
    companyfacts_by_security: dict[str, dict]
    bytes_transferred: int  # sum across both archives — for logging/observability only
    edgar_source: BulkEdgarSource | None = None
    """Wraps the real ``EdgarClient`` so steps 4-8 read `submissions.json`/`companyfacts.json`
    bodies already streamed here instead of re-fetching per security. ``None``
    when no ``edgar_client`` was supplied to :meth:`BootstrapRunner.fetch_bulk_archives`
    (e.g. in tests that only exercise the raw archive-streaming behaviour)."""


@dataclass
class BootstrapReport:
    filer_profile_mismatches: list[str]
    sessions_scored: int
    sessions_meeting_security_threshold: int
    """Count of replayed sessions with >=:data:`MIN_SCORED_SECURITIES` securities
    scored (arc42 §6.3 step 12: ">=85 securities on >=370/378 sessions") —
    what :meth:`BootstrapRunner.validate` actually gates on."""
    portfolio_binding: BindingValidationResult | None
    validation_passed: bool
    cik_mismatches: list[str] = field(default_factory=list)
    performance_metrics: list[PerformanceMetric] = field(default_factory=list)
    bytes_transferred: int = 0


def raw_backfill_start(today: date) -> date:
    return today - timedelta(days=RAW_BACKFILL_MONTHS * 30)


def extraction_backfill_start(today: date) -> date:
    return today - timedelta(days=EXTRACTION_BACKFILL_MONTHS * 30)


class BootstrapRunner:
    def __init__(self, *, universe: Universe, context_factory) -> None:
        """``context_factory(as_of_date) -> PipelineContext`` builds a fresh
        pipeline context (fresh scratch state) for a given backfill date,
        sharing the same repositories/providers across the whole window."""

        self._universe = universe
        self._context_factory = context_factory

    async def verify_cik_mappings(self, company_tickers: dict) -> list[str]:
        """Step 1: verify each security's configured CIK against EDGAR's
        ``company_tickers.json`` (arc42 §6.3 step 1). ``company_tickers`` is
        the raw parsed JSON, ``{"0": {"cik_str": int, "ticker": str, "title":
        str}, ...}``.

        Soft-logs (never raises) a mismatch — mirrors :meth:`verify_filer_profiles`:
        a stale ticker->CIK mapping in ``config/universe.yaml`` is a
        data-quality signal to review, not a reason to abort a multi-hour
        bootstrap run.
        """

        cik_by_ticker: dict[str, str] = {}
        for entry in company_tickers.values():
            ticker = entry.get("ticker")
            cik_str = entry.get("cik_str")
            if ticker and cik_str is not None:
                cik_by_ticker[ticker.upper()] = f"{int(cik_str):010d}"

        mismatches: list[str] = []
        for sec in self._universe.securities:
            edgar_cik = cik_by_ticker.get(sec.ticker.upper())
            if edgar_cik is None:
                mismatches.append(f"{sec.ticker}: not found in EDGAR company_tickers.json")
            elif edgar_cik != sec.cik:
                mismatches.append(f"{sec.ticker}: config CIK {sec.cik} != EDGAR CIK {edgar_cik}")
        return mismatches

    async def verify_filer_profiles(
        self,
        edgar_client=None,
        submissions_by_security: dict[str, dict] | None = None,
    ) -> list[str]:
        """Step 2: verify `filer_profile` against EDGAR formType history."""

        mismatches: list[str] = []
        for sec in self._universe.securities:
            submissions = (
                submissions_by_security.get(sec.id)
                if submissions_by_security is not None
                else None
            )
            if submissions is None:
                if edgar_client is None:
                    mismatches.append(f"{sec.ticker}: no EDGAR submissions record")
                    continue
                submissions = await edgar_client.get_submissions(sec.cik)
            forms = set(submissions.get("filings", {}).get("recent", {}).get("form", []))
            files_domestic_forms = bool(forms & {"10-K", "10-Q"})
            files_fpi_forms = bool(forms & {"20-F", "6-K"})
            observed = FilerProfile.FPI if files_fpi_forms and not files_domestic_forms else FilerProfile.DOMESTIC
            if observed != sec.filer_profile:
                mismatches.append(
                    f"{sec.ticker}: config says {sec.filer_profile.value}, "
                    f"EDGAR formType history implies {observed.value}"
                )
        return mismatches

    async def fetch_bulk_archives(
        self,
        *,
        user_agent: str,
        rate_limit_per_second: float = 8.0,
        blob_sink: RawArtefactSink | None = None,
        client=None,
        edgar_client: EdgarClient | None = None,
    ) -> BulkArchiveResult:
        """Step 3: stream submissions.zip + companyfacts.zip via HTTP Range
        requests (:mod:`auspex.providers.edgar_bulk`) and extract only the
        configured universe securities' CIK entries.

        Neither archive is ever downloaded in full or written to local
        ephemeral storage — only the selected entries are transferred, and
        only those are persisted, as raw artefacts, via ``blob_sink`` (arc42
        §6.3 step 3: "persist only selected universe records/raw
        artefacts"). Avoids one incremental HTTP call per security for the
        full-history window; the per-CIK endpoints remain what the nightly
        collectors use for incremental deltas.

        ``client`` is an optional pre-built ``httpx.Client`` — production
        code leaves it unset (each archive gets its own real client); tests
        inject an ``httpx.MockTransport``-backed client to avoid any real
        network access.

        ``edgar_client``, when supplied, is wrapped in a
        :class:`~auspex.providers.edgar_bulk.BulkEdgarSource` served from the
        just-extracted per-security records and returned as
        ``BulkArchiveResult.edgar_source`` — steps 4-8 install it as
        ``ctx.providers.edgar_client`` so the shared nightly collectors read
        ``submissions.json``/``companyfacts.json`` bodies already streamed
        here instead of one incremental HTTP call per security.
        """

        from auspex.providers.edgar_bulk import (
            COMPANYFACTS_BULK_URL,
            SUBMISSIONS_BULK_URL,
            extract_universe_cik_documents,
            open_remote_bulk_zip,
        )

        ciks = [sec.cik for sec in self._universe.securities]
        cik_to_security_id = {sec.cik: sec.id for sec in self._universe.securities}

        submissions_archive = await open_remote_bulk_zip(
            SUBMISSIONS_BULK_URL, user_agent=user_agent, rate_limit_per_second=rate_limit_per_second, client=client
        )
        try:
            submissions_by_cik = await extract_universe_cik_documents(submissions_archive, ciks)
        finally:
            submissions_archive.close()

        companyfacts_archive = await open_remote_bulk_zip(
            COMPANYFACTS_BULK_URL, user_agent=user_agent, rate_limit_per_second=rate_limit_per_second, client=client
        )
        try:
            companyfacts_by_cik = await extract_universe_cik_documents(companyfacts_archive, ciks)
        finally:
            companyfacts_archive.close()

        submissions_by_security = {cik_to_security_id[cik]: doc for cik, doc in submissions_by_cik.items()}
        companyfacts_by_security = {cik_to_security_id[cik]: doc for cik, doc in companyfacts_by_cik.items()}

        if blob_sink is not None:
            for security_id, doc in submissions_by_security.items():
                await blob_sink.upload_document_blob(security_id, "bulk-submissions", "json", json.dumps(doc))
            for security_id, doc in companyfacts_by_security.items():
                await blob_sink.upload_document_blob(security_id, "bulk-companyfacts", "json", json.dumps(doc))

        bytes_transferred = submissions_archive.bytes_fetched + companyfacts_archive.bytes_fetched
        logger.info(
            "bootstrap: bulk archives streamed via HTTP range requests — "
            "%d/%d submissions, %d/%d companyfacts, %d bytes transferred (archives never downloaded in full)",
            len(submissions_by_cik),
            len(ciks),
            len(companyfacts_by_cik),
            len(ciks),
            bytes_transferred,
        )

        edgar_source = None
        if edgar_client is not None:
            edgar_source = BulkEdgarSource(
                edgar_client, submissions_by_cik=submissions_by_cik, companyfacts_by_cik=companyfacts_by_cik
            )

        return BulkArchiveResult(
            submissions_by_security=submissions_by_security,
            companyfacts_by_security=companyfacts_by_security,
            bytes_transferred=bytes_transferred,
            edgar_source=edgar_source,
        )

    async def backfill_prices_fx(self, ctx: PipelineContext) -> None:
        """Step 4: seed every security's price watermark (and the shared FX
        watermark) to the 36-month raw backfill start, then run the existing
        nightly price/FX collectors once (arc42 §6.3 step 4 — "Two windows,
        not one": 36 months of raw prices/FX)."""

        seed = (raw_backfill_start(ctx.as_of_date) - timedelta(days=1)).isoformat()
        for sec in self._universe.securities:
            key = watermark_key("price", sec.id)
            if await ctx.repos.watermarks.get_watermark(key) is None:
                await ctx.repos.watermarks.set_watermark(key, seed)
        from auspex.collectors.fx_collector import fx_watermark_key

        for pair in ctx.config["weights"].get(
            "valuation_fx_pairs",
            ["USDCHF"],
        ):
            fx_key = watermark_key(
                _FX_COLLECTOR_NAME,
                fx_watermark_key(pair),
            )
            if await ctx.repos.watermarks.get_watermark(fx_key) is None:
                await ctx.repos.watermarks.set_watermark(fx_key, seed)

        manifest = new_manifest(ctx.as_of_date, run_type="bootstrap")
        await step_collect_prices(ctx, manifest)
        await step_collect_fx(ctx, manifest)

    async def backfill_filings(self, ctx: PipelineContext, bulk_source) -> None:
        """Step 5: download filing documents for the 18-month extraction
        window (arc42 §6.3 step 5) — bounds the shared ``FilingCollector`` to
        only filings at or after the 18-month floor via
        ``BulkEdgarSource.floor_date`` (no per-CIK incremental HTTP calls;
        ``bulk_source`` is ``None`` when step 3 ran without a real
        ``edgar_client``, in which case this falls back to whatever
        ``ctx.providers.edgar_client`` is already configured with)."""

        if bulk_source is not None:
            bulk_source.floor_date = extraction_backfill_start(ctx.as_of_date)
        manifest = new_manifest(ctx.as_of_date, run_type="bootstrap")
        await step_collect_filings(ctx, manifest)
        if bulk_source is not None and hasattr(bulk_source, "latest_accession"):
            for sec in self._universe.securities:
                accession = await bulk_source.latest_accession(
                    sec.cik, INTERESTING_FORMS
                )
                if accession is not None:
                    await ctx.repos.watermarks.set_watermark(
                        watermark_key("filing", sec.id), accession
                    )

    async def backfill_form4(self, ctx: PipelineContext, bulk_source) -> None:
        """Step 6: download and parse ALL Form 4 XML across the 36-month raw
        window (arc42 §6.3 step 6) — deterministic, no LLM."""

        if bulk_source is not None:
            bulk_source.floor_date = raw_backfill_start(ctx.as_of_date)
        manifest = new_manifest(ctx.as_of_date, run_type="bootstrap")
        await step_collect_insiders(ctx, manifest)
        if bulk_source is not None and hasattr(
            bulk_source, "latest_filing_date"
        ):
            for sec in self._universe.securities:
                filed_date = await bulk_source.latest_filing_date(sec.cik, {"4"})
                if filed_date is not None:
                    await ctx.repos.watermarks.set_watermark(
                        watermark_key("insider", sec.id), filed_date.isoformat()
                    )

    async def extract_and_collect_fundamentals(
        self, ctx: PipelineContext, *, include_fundamentals: bool = True
    ) -> None:
        """Step 7: run Channel A + B extraction over the 18-month document
        set just collected in step 5, then collect fundamentals for the
        resulting 10-K/10-Q/20-F accessions (arc42 §6.3 step 7).

        Fundamentals collection must run *after* filings (step 5) — it
        derives its accessions from ``ctx.new_document_ids_by_security``,
        which only steps 5/6 populate.
        """

        floor = extraction_backfill_start(ctx.as_of_date)
        documents = await fetch_all(ctx.repos.document_sink)
        eligible_forms = {"10-K", "10-Q", "8-K", "20-F", "6-K", "S-1"}
        fundamental_forms = {"10-K", "10-Q", "20-F"}
        ctx.new_document_ids_by_security = {}
        ctx.new_accessions_by_security = {}
        for document in documents:
            if (
                document.knowledge_date < floor
                or document.form_type not in eligible_forms
            ):
                continue
            ctx.new_document_ids_by_security.setdefault(document.security_id, []).append(
                document.id
            )
            if (
                document.form_type in fundamental_forms
                and document.accession_number
            ):
                ctx.new_accessions_by_security.setdefault(document.security_id, set()).add(
                    document.accession_number
                )
        logger.info(
            "bootstrap extraction workset rebuilt from persistence — documents=%d, securities=%d",
            sum(len(ids) for ids in ctx.new_document_ids_by_security.values()),
            len(ctx.new_document_ids_by_security),
        )

        manifest = new_manifest(ctx.as_of_date, run_type="bootstrap")
        await step_extract_channel_a(ctx, manifest)
        await step_extract_channel_b(ctx, manifest)
        if include_fundamentals:
            await step_collect_fundamentals(ctx, manifest)

    async def backfill_news(self, ctx: PipelineContext) -> None:
        """Step 8: backfill news for whatever window the provider licence
        permits (arc42 §6.3 step 8, typically <=12 months) — seeds every
        security's news watermark to the 18-month extraction floor; the
        provider itself is free to return less if its licence window is
        shorter, degrading that security rather than aborting the run."""

        seed = datetime.combine(extraction_backfill_start(ctx.as_of_date), time.min).isoformat()
        for sec in self._universe.securities:
            await ctx.repos.watermarks.set_watermark(watermark_key("news", sec.id), seed)
        manifest = new_manifest(ctx.as_of_date, run_type="bootstrap")
        await step_collect_news(ctx, manifest)

    async def replay_scoring(
        self,
        start_date: date,
        end_date: date,
        completed_dates: set[date] | None = None,
    ) -> tuple[int, int]:
        """Step 9: replay scoring day by day across the 18-month extraction
        window, chronologically (arc42 §6.3 step 9), writing scores with
        ``is_backfilled=True``.

        Runs the five scoring steps directly (COMPUTE_RAW_LEGS,
        ASSIGN_COHORTS, NORMALISE, DIFF, WRITE_SNAPSHOT) rather than the full
        20-step nightly pipeline — bootstrap already ran collection/extraction
        once for the whole window (steps 4-8), so re-running
        COLLECT_*/EXTRACT_* every replayed day would be redundant
        network/LLM work for data that never changes retroactively.

        Each day's context shares the same underlying repositories (so day
        N's ``WRITE_SNAPSHOT`` becomes day N+1's "prior day" comparison in
        ``DIFF``), but wraps ``repos.price_sink`` in :class:`_AsOfPriceSink`
        so ``pipeline.steps``'s latest-price lookup — which has no date
        bound of its own — only sees bars up to that day, restoring
        point-in-time correctness for the replay.

        Returns ``(sessions_scored, sessions_meeting_security_threshold)``:
        ``sessions_scored`` is every trading session actually replayed (an
        approximation of the ~378-session window — arc42 §6.3 step 12
        references ~378 trading sessions in 18 months); ``sessions_meeting_security_threshold``
        counts only those sessions where at least :data:`MIN_SCORED_SECURITIES`
        (85) securities were scored — this, not merely the last session's
        count, is what :meth:`validate` (step 12: ">=85 securities on
        >=370/378 sessions") checks against :data:`MIN_SESSIONS_SCORED` (370).
        """

        sessions_scored = 0
        sessions_meeting_security_threshold = 0
        preload_ctx = self._context_factory(start_date)
        preloaded_documents = await fetch_all(preload_ctx.repos.document_sink)
        preloaded_extractions = (
            await fetch_all(preload_ctx.repos.channel_a_sink)
            if preload_ctx.repos.channel_a_sink is not None
            else []
        )
        preloaded_fundamentals = await fetch_all(preload_ctx.repos.fundamental_sink)
        from auspex.currency.table import PointInTimeFxTable

        fx_converter = PointInTimeFxTable(
            await fetch_all(preload_ctx.repos.fx_sink)
        )
        replay_dates = {
            item
            for item in _each_day(start_date, end_date)
            if item.weekday() < 5 and item not in (completed_dates or set())
        }
        relevant_score_dates = {
            item - timedelta(days=offset)
            for item in replay_dates
            for offset in (1, 7)
        } - replay_dates
        preloaded_scores = []
        if preload_ctx.repos.score_repo is not None:
            if hasattr(preload_ctx.repos.score_repo, "for_dates"):
                preloaded_scores = await preload_ctx.repos.score_repo.for_dates(
                    relevant_score_dates
                )
            else:
                preloaded_scores = [
                    snapshot
                    for snapshot in await fetch_all(preload_ctx.repos.score_repo)
                    if snapshot.as_of_date in relevant_score_dates
                ]
        replay_score_repo = (
            _ReplayScoreRepository(preload_ctx.repos.score_repo, preloaded_scores)
            if preload_ctx.repos.score_repo is not None
            else None
        )
        security_ids = [security.id for security in self._universe.securities]
        current = start_date
        while current <= end_date:
            if current.weekday() < 5 and current not in (completed_dates or set()):
                day_ctx = self._context_factory(current)
                day_ctx.repos = dataclasses.replace(
                    day_ctx.repos,
                    price_sink=_AsOfPriceSink(day_ctx.repos.price_sink, current, security_ids),
                    score_repo=replay_score_repo,
                )
                day_ctx.__dict__["_preloaded_documents"] = preloaded_documents
                day_ctx.__dict__["_preloaded_extractions"] = preloaded_extractions
                day_ctx.__dict__["_preloaded_fundamentals"] = preloaded_fundamentals
                day_ctx.__dict__["_fx_converter"] = fx_converter
                manifest = new_manifest(current, run_type="bootstrap")
                await step_compute_raw_legs(day_ctx, manifest)
                await step_assign_cohorts(day_ctx, manifest)
                await step_normalise(day_ctx, manifest)
                await step_diff(day_ctx, manifest)
                await step_write_snapshot(day_ctx, manifest)

                snapshots = day_ctx.__dict__.get("_snapshots", [])
                for snapshot in snapshots:
                    snapshot.is_backfilled = True
                    if day_ctx.repos.score_repo is not None:
                        await day_ctx.repos.score_repo.upsert(snapshot)
                sessions_scored += 1
                if (
                    sum(1 for snapshot in snapshots if snapshot.percentile is not None)
                    >= MIN_SCORED_SECURITIES
                ):
                    sessions_meeting_security_threshold += 1
                if replay_score_repo is not None:
                    replay_score_repo.discard_before(
                        current - timedelta(days=7)
                    )
            current += timedelta(days=1)
        return sessions_scored, sessions_meeting_security_threshold

    async def existing_replay_coverage(
        self, ctx: PipelineContext, start_date: date, end_date: date
    ) -> tuple[int, int, set[date]]:
        if ctx.repos.score_repo is None:
            return 0, 0, set()
        if hasattr(ctx.repos.score_repo, "valid_score_counts_by_date"):
            raw_counts = await ctx.repos.score_repo.valid_score_counts_by_date(
                start_date, end_date
            )
            counts = {
                as_of_date: set(range(count))
                for as_of_date, count in raw_counts.items()
            }
        else:
            snapshots = await fetch_all(ctx.repos.score_repo)
            counts: dict[date, set[str]] = {}
            for snapshot in snapshots:
                if (
                    start_date <= snapshot.as_of_date <= end_date
                    and snapshot.is_backfilled
                    and snapshot.percentile is not None
                ):
                    counts.setdefault(snapshot.as_of_date, set()).add(
                        snapshot.security_id
                    )
        completed_dates = {
            as_of_date
            for as_of_date, security_ids in counts.items()
            if len(security_ids) >= MIN_SCORED_SECURITIES
        }
        return len(counts), len(completed_dates), completed_dates

    async def securities_requiring_full_replay(
        self,
        ctx: PipelineContext,
        start_date: date,
        end_date: date,
    ) -> set[str]:
        """Return universe members without a sufficiently complete historical score series.

        A universe expansion must replay every date, even when the old universe
        already satisfied the aggregate >=85 acceptance threshold, because
        cohort normalization changes for every member of the affected cohort.
        """

        if ctx.repos.score_repo is None:
            return {security.id for security in self._universe.securities}
        security_ids = [security.id for security in self._universe.securities]
        counts: dict[str, int] = {}
        raw_query = getattr(ctx.repos.score_repo, "raw_query", None)
        if raw_query is not None:
            semaphore = asyncio.Semaphore(16)

            async def count_security(security_id: str) -> tuple[str, int]:
                async with semaphore:
                    rows = await raw_query(
                        (
                            "SELECT VALUE COUNT(1) FROM c "
                            "WHERE c.security_id=@security_id "
                            "AND c.as_of_date>=@start AND c.as_of_date<=@end "
                            "AND c.is_backfilled=true"
                        ),
                        [
                            {"name": "@security_id", "value": security_id},
                            {"name": "@start", "value": start_date.isoformat()},
                            {"name": "@end", "value": end_date.isoformat()},
                        ],
                        partition_key=security_id,
                    )
                    return security_id, int(rows[0]) if rows else 0

            counts = dict(
                await asyncio.gather(
                    *(count_security(security_id) for security_id in security_ids)
                )
            )
        else:
            for snapshot in await fetch_all(ctx.repos.score_repo):
                if (
                    snapshot.security_id in security_ids
                    and start_date <= snapshot.as_of_date <= end_date
                    and snapshot.is_backfilled
                ):
                    counts[snapshot.security_id] = (
                        counts.get(snapshot.security_id, 0) + 1
                    )
        return {
            security_id
            for security_id in security_ids
            if counts.get(security_id, 0) < MIN_SESSIONS_SCORED
        }

    async def compute_performance_metrics(
        self,
        ctx: PipelineContext,
        scored_dates: list[date] | None = None,
        performance_repo=None,
        accepted_recommendation_ids: set[str] | None = None,
        attribution_user_id: str | None = None,
        include_recommendation_metrics: bool = True,
    ) -> list[PerformanceMetric]:
        """Step 10: compute performance metrics over the just-replayed
        history (arc42 §5.8, §6.3 step 10) — one composite/leg IC per
        horizon, built from the ``ScoreSnapshot``\\ s step 9 just wrote and
        the *full* price history (not the as-of-bounded proxy step 9 uses for
        scoring itself; forward returns necessarily look past ``as_of_date``).

        ``scored_dates`` may be omitted: this is what the weekly
        ``job-auspex-performance`` job (arc42 §5.8) does, since it has no
        replay window of its own to track — every distinct ``as_of_date``
        already present in ``ctx.repos.score_repo`` is used instead, so the
        job recomputes metrics over the full history the nightly pipeline
        and/or bootstrap have accumulated so far.

        Score-derived metrics (composite/leg IC, leg correlation, cohort
        quality) are population-level measurements of the research itself and
        are shared by every user. Recommendation-derived metrics (suggestion
        hit rate, disposition outcome) are *attribution*, and attribution is
        private: with more than one user, blending everyone's recommendations
        into the shared ``performance`` container would both double-count the
        same underlying decision and expose one user's behaviour to another.
        ``attribution_user_id`` therefore scopes those metrics to a single
        user's own recommendations; when it is omitted the behaviour is
        unchanged (single-owner deployments and bootstrap replay).
        """

        all_bars = exclude_quarantined(await fetch_all(ctx.repos.price_sink))
        bars_by_security: dict[str, list] = {}
        for bar in all_bars:
            bars_by_security.setdefault(bar.security_id, []).append(bar)
        for bars in bars_by_security.values():
            bars.sort(key=lambda b: b.session_date)
        dates_by_security = {
            security_id: [bar.session_date for bar in bars]
            for security_id, bars in bars_by_security.items()
        }

        all_snapshots = await fetch_all(ctx.repos.score_repo)
        logger.info(
            "bootstrap performance inputs — price_bars=%d, score_snapshots=%d, snapshots_with_percentile=%d",
            len(all_bars),
            len(all_snapshots),
            sum(1 for snapshot in all_snapshots if snapshot.percentile is not None),
        )
        if scored_dates is None:
            scored_dates = sorted({snap.as_of_date for snap in all_snapshots})
        snapshots_by_date: dict[date, list] = {}
        for snap in all_snapshots:
            if snap.as_of_date in scored_dates:
                snapshots_by_date.setdefault(snap.as_of_date, []).append(snap)

        cross_sections: list[DateCrossSection] = []
        for as_of in sorted(snapshots_by_date):
            candidates = [s for s in snapshots_by_date[as_of] if s.percentile is not None]
            for horizon in HORIZONS:
                pairs = [
                    (snapshot, forward_return)
                    for snapshot in candidates
                    if (
                        forward_return := _forward_return_usd(
                            bars_by_security,
                            snapshot.security_id,
                            as_of,
                            horizon,
                            dates_by_security,
                        )
                    )
                    is not None
                ]
                if not pairs:
                    continue
                snaps = [pair[0] for pair in pairs]
                leg_z_by_leg: dict[LegName, list[Decimal]] = {}
                leg_z_by_security: dict[LegName, dict[str, Decimal]] = {}
                for leg in LegName:
                    leg_results = [snapshot.legs.get(leg) for snapshot in snaps]
                    available = {
                        snapshot.security_id: Decimal(result.z)
                        for snapshot, result in zip(snaps, leg_results, strict=True)
                        if result is not None and result.z is not None
                    }
                    if available:
                        leg_z_by_security[leg] = available
                    if all(result is not None and result.z is not None for result in leg_results):
                        leg_z_by_leg[leg] = [
                            Decimal(result.z) for result in leg_results if result is not None
                        ]
                cross_sections.append(
                    DateCrossSection(
                        as_of_date=as_of,
                        security_ids=[snapshot.security_id for snapshot in snaps],
                        percentiles=[Decimal(snapshot.percentile) for snapshot in snaps],
                        leg_z_by_leg=leg_z_by_leg,
                        forward_returns_usd_by_horizon={
                            horizon: [pair[1] for pair in pairs]
                        },
                        leg_z_by_security=leg_z_by_security,
                        coverage_by_security={
                            snapshot.security_id: Decimal(snapshot.coverage) for snapshot in snaps
                        },
                        trailing_returns_usd_by_window={
                            63: {
                                snapshot.security_id: trailing
                                for snapshot in snaps
                                if (
                                    trailing := _trailing_return_usd(
                                        bars_by_security,
                                        snapshot.security_id,
                                        as_of,
                                        63,
                                        dates_by_security,
                                    )
                                )
                                is not None
                            }
                        },
                    )
                )

        if not cross_sections:
            logger.warning("bootstrap performance produced no eligible forward-return cross-sections")
            return []

        metrics = (
            compute_composite_ic_metrics(cross_sections)
            + compute_leg_ic_metrics(cross_sections)
            + compute_detailed_metrics(
                cross_sections,
                cost_per_unit_turnover=Decimal(
                    str(
                        ctx.config.get("fees", {}).get(
                            "performance_round_trip_cost_rate",
                            "0.0050",
                        )
                    )
                ),
            )
        )
        if include_recommendation_metrics and ctx.repos.recommendation_repo is not None:
            if attribution_user_id is not None:
                if hasattr(ctx.repos.recommendation_repo, "raw_query"):
                    recommendations = await ctx.repos.recommendation_repo.query(
                        query="SELECT * FROM c WHERE c.user_id=@user_id",
                        parameters=[{"name": "@user_id", "value": attribution_user_id}],
                        partition_key=attribution_user_id,
                    )
                else:
                    recommendations = [
                        recommendation
                        for recommendation in await fetch_all(
                            ctx.repos.recommendation_repo
                        )
                        if recommendation.user_id == attribution_user_id
                    ]
            else:
                recommendations = await fetch_all(ctx.repos.recommendation_repo)
            snapshot_by_key = {
                (snapshot.as_of_date, snapshot.security_id): snapshot
                for snapshot in all_snapshots
            }
            accepted_ids = accepted_recommendation_ids or set()
            outcomes: list[SuggestionOutcome] = []
            disposition_outcomes: list[DispositionOutcome] = []
            for recommendation in recommendations:
                if recommendation.action not in {
                    Action.BUY,
                    Action.ADD,
                    Action.TRIM,
                    Action.SELL,
                }:
                    continue
                snapshot = snapshot_by_key.get(
                    (recommendation.as_of_date, recommendation.security_id)
                )
                if snapshot is None:
                    continue
                security_return = _forward_return_usd(
                    bars_by_security,
                    recommendation.security_id,
                    recommendation.as_of_date,
                    126,
                    dates_by_security,
                )
                if security_return is None:
                    continue
                cohort_returns = [
                    forward_return
                    for candidate in snapshots_by_date.get(recommendation.as_of_date, [])
                    if candidate.cohort_used == snapshot.cohort_used
                    and (
                        forward_return := _forward_return_usd(
                            bars_by_security,
                            candidate.security_id,
                            recommendation.as_of_date,
                            126,
                            dates_by_security,
                        )
                    )
                    is not None
                ]
                if not cohort_returns:
                    continue
                ordered_returns = sorted(cohort_returns)
                midpoint = len(ordered_returns) // 2
                cohort_median = (
                    ordered_returns[midpoint]
                    if len(ordered_returns) % 2
                    else (
                        ordered_returns[midpoint - 1] + ordered_returns[midpoint]
                    )
                    / Decimal(2)
                )
                if recommendation.action in {Action.TRIM, Action.SELL}:
                    security_return = -security_return
                    cohort_median = -cohort_median
                outcome = SuggestionOutcome(
                    security_return_usd=security_return,
                    cohort_median_return_usd=cohort_median,
                )
                outcomes.append(outcome)
                disposition_outcomes.append(
                    DispositionOutcome(
                        security_return_usd=security_return,
                        cohort_median_return_usd=cohort_median,
                        accepted=recommendation.id in accepted_ids,
                    )
                )
            suggestion_metric = compute_suggestion_hit_rate_metric(
                max(scored_dates),
                outcomes,
            )
            if suggestion_metric is not None:
                metrics.append(suggestion_metric)
            for accepted in (True, False):
                disposition_metric = compute_disposition_outcome_metric(
                    max(scored_dates),
                    disposition_outcomes,
                    accepted=accepted,
                )
                if disposition_metric is not None:
                    metrics.append(disposition_metric)
        if performance_repo is not None:
            for metric in metrics:
                await performance_repo.upsert(metric)
        logger.info(
            "bootstrap performance complete — cross_sections=%d, metrics=%d",
            len(cross_sections),
            len(metrics),
        )
        return metrics

    async def bind_and_validate_portfolio(
        self, adapter: PortfolioAdapter, as_of: date, *, confirmed: bool
    ) -> BindingValidationResult:
        """Step 11: bind the existing portfolio ledger read-only, validate it,
        log the mapped sample document and binding summary for owner review,
        and require explicit confirmation before bootstrap proceeds.

        An unmapped ticker is a **hard failure** (arc42 §5.7): a position
        Auspex cannot see is a position it cannot advise on. This never
        writes anything — it only reads through :class:`PortfolioAdapter`.

        ``confirmed`` is required (no default) so every call site must make
        an explicit choice rather than accidentally inheriting a
        default-true value. In production it is wired from
        ``Settings.confirm_portfolio_binding`` /
        ``AUSPEX_CONFIRM_PORTFOLIO_BINDING``, which itself defaults to
        ``False``. When ``confirmed`` is not ``True``, this raises
        :class:`PortfolioBindingNotConfirmedError` *after* logging the
        mapping so the operator has exactly what they need to review before
        re-running with the flag set.
        """

        result = await validate_portfolio_binding(adapter, self._universe, as_of)

        logger.info("bootstrap: portfolio binding — mapped sample document: %s", result.sample_document)
        logger.info(
            "bootstrap: portfolio binding summary — lot_level=%s, cash_chf=%s, holdings=%d, unmapped_tickers=%s",
            result.snapshot.lot_level,
            result.snapshot.cash_chf,
            len(result.snapshot.holdings),
            result.unmapped_tickers or "none",
        )
        if result.unmapped_tickers:
            logger.error(
                "bootstrap: %d holding(s) do not map to config/universe.yaml: %s",
                len(result.unmapped_tickers),
                result.unmapped_tickers,
            )

        if not confirmed:
            raise PortfolioBindingNotConfirmedError(
                "portfolio binding not confirmed for this run — review the mapped sample document and "
                "binding summary logged above, then set AUSPEX_CONFIRM_PORTFOLIO_BINDING=true "
                "(Settings.confirm_portfolio_binding) and re-run bootstrap. This never defaults to confirmed."
            )

        return result

    def validate(self, sessions_scored: int, sessions_meeting_security_threshold: int) -> bool:
        """Step 12: >=85 securities scored on >=370 of the last 378 sessions.

        ``sessions_meeting_security_threshold`` (from :meth:`replay_scoring`)
        is the count of replayed sessions where at least
        :data:`MIN_SCORED_SECURITIES` securities were actually scored — the
        gate is on that count reaching :data:`MIN_SESSIONS_SCORED`, not on
        the last session alone.
        """

        return sessions_meeting_security_threshold >= MIN_SESSIONS_SCORED

    async def run(
        self,
        *,
        as_of_date: date,
        company_tickers: dict,
        edgar_client: EdgarClient,
        user_agent: str,
        portfolio_adapter: PortfolioAdapter,
        confirmed: bool,
        rate_limit_per_second: float = 8.0,
        blob_sink: RawArtefactSink | None = None,
        client=None,
        performance_repo=None,
    ) -> BootstrapReport:
        """Run the full arc42 §6.3 cold-start sequence end to end (steps 1-12).

        ``as_of_date`` is "today" for this run — steps 4/6 raw-backfill from
        ``raw_backfill_start(as_of_date)`` (36 months back), steps 5/7/8
        extraction-backfill from ``extraction_backfill_start(as_of_date)`` (18
        months back), and step 9 replays every trading day in
        ``[extraction_backfill_start(as_of_date), as_of_date]``.

        Step 11 (portfolio binding) deliberately runs early, right after
        steps 1-2, rather than at its arc42 §6.3 position at the very end:
        it is a cheap, read-only check (:class:`PortfolioAdapter` only) that
        always logs the mapped sample document and binding summary for
        operator review *before* raising, regardless of ``confirmed``. Bootstrap
        is a single 2.5-5 hour run-once job (arc42 §6.3 "Runtime budget");
        gating on a static, precomputed confirmation flag *before* the
        expensive bulk fetch/backfill/extraction/scoring (steps 3-10) avoids
        burning hours of EDGAR/Channel A+B work only to discover at the very
        end that ``AUSPEX_CONFIRM_PORTFOLIO_BINDING`` was never set.
        ``PortfolioBindingNotConfirmedError`` propagates uncaught so the
        caller's exit code reflects the missing confirmation.
        """

        cik_mismatches = await self.verify_cik_mappings(company_tickers)
        portfolio_binding = await self.bind_and_validate_portfolio(portfolio_adapter, as_of_date, confirmed=confirmed)

        bulk_result = await self.fetch_bulk_archives(
            user_agent=user_agent,
            rate_limit_per_second=rate_limit_per_second,
            blob_sink=blob_sink,
            client=client,
            edgar_client=edgar_client,
        )
        filer_profile_mismatches = await self.verify_filer_profiles(
            submissions_by_security=bulk_result.submissions_by_security
        )

        seed_ctx = self._context_factory(as_of_date)
        seed_ctx.providers.edgar_client = bulk_result.edgar_source or edgar_client

        await self.backfill_prices_fx(seed_ctx)
        await self.backfill_filings(seed_ctx, bulk_result.edgar_source)
        await self.backfill_form4(seed_ctx, bulk_result.edgar_source)
        await self.extract_and_collect_fundamentals(seed_ctx)
        await self.backfill_news(seed_ctx)

        start_date = extraction_backfill_start(as_of_date)
        (
            sessions_scored,
            sessions_meeting_security_threshold,
            completed_dates,
        ) = await self.existing_replay_coverage(seed_ctx, start_date, as_of_date)
        incomplete_security_ids = await self.securities_requiring_full_replay(
            seed_ctx,
            start_date,
            as_of_date,
        )
        if incomplete_security_ids:
            logger.info(
                "bootstrap: forcing full replay for universe parity — incomplete securities=%s",
                sorted(incomplete_security_ids),
            )
        if (
            sessions_meeting_security_threshold < MIN_SESSIONS_SCORED
            or incomplete_security_ids
        ):
            await self.replay_scoring(
                start_date,
                as_of_date,
                completed_dates=set() if incomplete_security_ids else completed_dates,
            )
            (
                sessions_scored,
                sessions_meeting_security_threshold,
                _,
            ) = await self.existing_replay_coverage(
                seed_ctx, start_date, as_of_date
            )

        performance_ctx = self._context_factory(as_of_date)
        scored_dates = [d for d in _each_day(start_date, as_of_date) if d.weekday() < 5]
        performance_metrics = await self.compute_performance_metrics(
            performance_ctx, scored_dates, performance_repo=performance_repo
        )

        conflicting_cik_mismatches = [
            mismatch for mismatch in cik_mismatches if "config CIK" in mismatch
        ]
        validation_passed = (
            self.validate(sessions_scored, sessions_meeting_security_threshold)
            and not conflicting_cik_mismatches
            and not filer_profile_mismatches
            and portfolio_binding.is_valid
            and bool(performance_metrics)
        )
        logger.info(
            "bootstrap: complete — sessions_scored=%d/%d, sessions_meeting_security_threshold(>=%d securities)=%d/%d, "
            "cik_mismatches=%d, filer_profile_mismatches=%d, performance_metrics=%d, validation_passed=%s",
            sessions_scored,
            TOTAL_RECENT_SESSIONS,
            MIN_SCORED_SECURITIES,
            sessions_meeting_security_threshold,
            MIN_SESSIONS_SCORED,
            len(cik_mismatches),
            len(filer_profile_mismatches),
            len(performance_metrics),
            validation_passed,
        )

        return BootstrapReport(
            filer_profile_mismatches=filer_profile_mismatches,
            sessions_scored=sessions_scored,
            sessions_meeting_security_threshold=sessions_meeting_security_threshold,
            portfolio_binding=portfolio_binding,
            validation_passed=validation_passed,
            cik_mismatches=cik_mismatches,
            performance_metrics=performance_metrics,
            bytes_transferred=bulk_result.bytes_transferred,
        )


def _each_day(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
