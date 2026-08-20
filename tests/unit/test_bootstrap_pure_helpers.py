"""Targeted unit tests for the pure/isolated helper logic in
`auspex.cli.bootstrap` that is not already covered by
`test_bootstrap_bulk_archives.py` (step 3: bulk archive streaming) or
`test_bootstrap_portfolio_binding.py` (step 11: confirmation gate).

Covers:
 - `raw_backfill_start`/`extraction_backfill_start` — the "two windows, not
   one" 36-month/18-month backfill math (arc42 §6.3).
 - `_forward_return_usd` — the forward-return lookup `compute_performance_metrics`
   (step 10) uses, including its `None`-on-missing-data boundary cases.
 - `_AsOfPriceSink` — the as-of date filter `replay_scoring` (step 9) wraps
   around the real price sink to restore point-in-time correctness.
 - `BootstrapRunner.validate` — the step 12 gate (">=85 securities scored on
   >=370 of the last 378 sessions"), including the corrected semantics that
   it counts *sessions meeting the security threshold*, not merely whether
   the last replayed session met it.
 - `BootstrapRunner.compute_performance_metrics`'s `scored_dates=None` mode —
   the weekly `job-auspex-performance` job (arc42 §5.8) has no replay window
   of its own, so it must derive every distinct `as_of_date` already present
   in `ctx.repos.score_repo` itself, rather than requiring a caller-supplied
   list (which only `BootstrapRunner.run()`'s own step 9/10 call site has).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from auspex.cli.bootstrap import (
    MIN_SESSIONS_SCORED,
    BootstrapRunner,
    _AsOfPriceSink,
    _forward_return_usd,
    _ReplayScoreRepository,
    extraction_backfill_start,
    raw_backfill_start,
)
from auspex.config.loader import Universe, load_universe
from auspex.models.enums import CohortConfidence, FilerProfile, LegName
from auspex.models.market import PriceBar
from auspex.models.performance import PerformanceMetric
from auspex.models.scoring import LegResult, ScoreSnapshot
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
from auspex.pipeline.context import PipelineContext, PipelineRepos


def make_bar(security_id: str, session_date: date, close_adjusted: str) -> PriceBar:
    return PriceBar(
        id=f"{security_id}:{session_date.isoformat()}",
        security_id=security_id,
        session_date=session_date,
        open_raw=close_adjusted,
        high_raw=close_adjusted,
        low_raw=close_adjusted,
        close_raw=close_adjusted,
        volume=1_000,
        close_adjusted=close_adjusted,
    )


class TestBackfillWindows:
    """arc42 §6.3 'Two windows, not one': 36 months raw vs 18 months
    documents/extraction/scoring — the raw window must start strictly
    earlier than the extraction window."""

    def test_raw_backfill_is_36_months_before_today(self):
        today = date(2026, 8, 8)
        assert raw_backfill_start(today) == today - timedelta(days=36 * 30)

    def test_extraction_backfill_is_18_months_before_today(self):
        today = date(2026, 8, 8)
        assert extraction_backfill_start(today) == today - timedelta(days=18 * 30)

    def test_raw_window_starts_strictly_before_extraction_window(self):
        today = date(2026, 8, 8)
        assert raw_backfill_start(today) < extraction_backfill_start(today)


class TestFilerProfileVerification:
    @pytest.mark.asyncio
    async def test_uses_bulk_submissions_without_live_edgar_calls(self):
        universe = load_universe()
        runner = BootstrapRunner(universe=universe, context_factory=lambda _: None)

        class FailingEdgar:
            async def get_submissions(self, cik):
                raise AssertionError("live EDGAR must not be called")

        submissions = {
            security.id: {
                "filings": {
                    "recent": {
                        "form": ["10-K", "10-Q"]
                        if security.filer_profile == FilerProfile.DOMESTIC
                        else ["20-F", "6-K"]
                    }
                }
            }
            for security in universe.securities
        }

        assert await runner.verify_filer_profiles(
            FailingEdgar(), submissions_by_security=submissions
        ) == []


class TestBootstrapWatermarkHandoff:
    @pytest.mark.asyncio
    async def test_form4_watermark_advances_to_unfiltered_bulk_tip(
        self, monkeypatch
    ):
        security = load_universe().securities[0]
        watermarks = InMemoryWatermarkStore()
        ctx = SimpleNamespace(
            as_of_date=date(2026, 8, 10),
            repos=SimpleNamespace(watermarks=watermarks),
        )

        class BulkSource:
            floor_date = None

            async def latest_filing_date(self, cik, forms):
                assert cik == security.cik
                assert forms == {"4"}
                return date(2026, 8, 9)

        async def skip_collection(ctx, manifest):
            return None

        monkeypatch.setattr(
            "auspex.cli.bootstrap.step_collect_insiders", skip_collection
        )
        runner = BootstrapRunner(
            universe=Universe(securities=[security]),
            context_factory=lambda _: ctx,
        )

        await runner.backfill_form4(ctx, BulkSource())

        assert (
            await watermarks.get_watermark(f"insider:{security.id}")
            == "2026-08-09"
        )


class TestForwardReturnUsd:
    def test_missing_security_returns_none(self):
        assert _forward_return_usd({}, "sec-nvda", date(2026, 1, 5), horizon_days=5) is None

    def test_no_bar_on_or_after_as_of_returns_none(self):
        bars = {
            "sec-nvda": [
                make_bar("sec-nvda", date(2026, 1, 1), "100"),
                make_bar("sec-nvda", date(2026, 1, 2), "101"),
            ]
        }
        # as_of is after every stored bar - there is no start bar to anchor on.
        assert _forward_return_usd(bars, "sec-nvda", date(2026, 2, 1), horizon_days=1) is None

    def test_insufficient_future_bars_returns_none(self):
        bars = {
            "sec-nvda": [
                make_bar("sec-nvda", date(2026, 1, 1), "100"),
                make_bar("sec-nvda", date(2026, 1, 2), "101"),
            ]
        }
        # horizon_days=5 would need index 0+5=5, but only 2 bars exist (indices 0-1).
        assert _forward_return_usd(bars, "sec-nvda", date(2026, 1, 1), horizon_days=5) is None

    def test_zero_start_close_returns_none(self):
        bars = {
            "sec-nvda": [
                make_bar("sec-nvda", date(2026, 1, 1), "0"),
                make_bar("sec-nvda", date(2026, 1, 2), "100"),
            ]
        }
        assert _forward_return_usd(bars, "sec-nvda", date(2026, 1, 1), horizon_days=1) is None

    def test_computes_return_over_horizon_from_first_bar_on_or_after_as_of(self):
        bars = {
            "sec-nvda": [
                make_bar("sec-nvda", date(2025, 12, 30), "90"),  # before as_of - ignored as start
                make_bar("sec-nvda", date(2026, 1, 1), "100"),  # first bar >= as_of -> start
                make_bar("sec-nvda", date(2026, 1, 2), "105"),
                make_bar("sec-nvda", date(2026, 1, 3), "110"),  # start_idx(1) + horizon(2) = 3 -> end
            ]
        }
        result = _forward_return_usd(bars, "sec-nvda", date(2026, 1, 1), horizon_days=2)
        assert result == pytest.approx(Decimal("0.1"))  # (110 - 100) / 100


class TestAsOfPriceSink:
    @pytest.mark.asyncio
    async def test_all_filters_out_bars_after_as_of(self):
        delegate = InMemoryPriceSink()
        await delegate.upsert_price_bar(make_bar("sec-nvda", date(2026, 1, 1), "100"))
        await delegate.upsert_price_bar(make_bar("sec-nvda", date(2026, 1, 5), "110"))
        await delegate.upsert_price_bar(make_bar("sec-nvda", date(2026, 1, 10), "120"))

        sink = _AsOfPriceSink(delegate, as_of=date(2026, 1, 5), security_ids=["sec-nvda"])
        bars = await sink.all()

        assert sorted(b.session_date for b in bars) == [date(2026, 1, 1), date(2026, 1, 5)]

    @pytest.mark.asyncio
    async def test_upsert_passes_straight_through_to_delegate(self):
        delegate = InMemoryPriceSink()
        sink = _AsOfPriceSink(delegate, as_of=date(2026, 1, 5), security_ids=["sec-nvda"])

        await sink.upsert_price_bar(make_bar("sec-nvda", date(2026, 1, 1), "100"))

        assert len(delegate.all()) == 1


class TestReplayScoreRepository:
    @pytest.mark.asyncio
    async def test_keeps_only_dates_needed_for_comparisons(self):
        delegate = InMemoryRepository()
        old = SimpleNamespace(id="old", as_of_date=date(2026, 1, 1))
        recent = SimpleNamespace(id="recent", as_of_date=date(2026, 1, 8))
        repository = _ReplayScoreRepository(delegate, [old, recent])

        repository.discard_before(date(2026, 1, 2))

        assert await repository.for_dates({date(2026, 1, 1)}) == []
        assert await repository.for_dates({date(2026, 1, 8)}) == [recent]

    @pytest.mark.asyncio
    async def test_upsert_is_available_by_date_and_persisted(self):
        delegate = InMemoryRepository()
        item = SimpleNamespace(id="score", as_of_date=date(2026, 1, 8))
        repository = _ReplayScoreRepository(delegate, [])

        await repository.upsert(item)

        assert await repository.for_dates({item.as_of_date}) == [item]
        assert await delegate.get(item.id) == item


class TestValidateGate:
    """arc42 §6.3 step 12: >=85 securities scored on >=370 of the last 378
    sessions — `validate` gates on `sessions_meeting_security_threshold`
    reaching `MIN_SESSIONS_SCORED` (370), not merely on `sessions_scored`
    (total replayed sessions) or the last session alone."""

    def _runner(self) -> BootstrapRunner:
        return BootstrapRunner(universe=Universe(securities=[]), context_factory=lambda d: None)

    def test_passes_when_threshold_met_exactly(self):
        runner = self._runner()
        assert runner.validate(sessions_scored=378, sessions_meeting_security_threshold=MIN_SESSIONS_SCORED) is True

    def test_fails_when_one_session_short_of_threshold(self):
        runner = self._runner()
        assert (
            runner.validate(sessions_scored=378, sessions_meeting_security_threshold=MIN_SESSIONS_SCORED - 1)
            is False
        )

    def test_fails_when_many_sessions_replayed_but_few_meet_security_threshold(self):
        """A high `sessions_scored` count alone must not satisfy the gate —
        it is specifically the count of sessions with >=85 securities scored
        that matters (the bug this test guards against: conflating "sessions
        replayed" with "sessions meeting the security-count bar")."""

        runner = self._runner()
        assert runner.validate(sessions_scored=378, sessions_meeting_security_threshold=50) is False

    def test_passes_when_threshold_exceeded(self):
        runner = self._runner()
        assert runner.validate(sessions_scored=378, sessions_meeting_security_threshold=378) is True

    @pytest.mark.asyncio
    async def test_existing_coverage_returns_only_dates_with_valid_percentiles(self):
        as_of = date(2026, 1, 5)
        valid = [
            make_snapshot(f"sec-{index}", as_of, index).model_copy(
                update={"is_backfilled": True}
            )
            for index in range(85)
        ]
        invalid = make_snapshot("sec-invalid", as_of + timedelta(days=1), 1).model_copy(
            update={"percentile": None, "is_backfilled": True}
        )
        repo = InMemoryRepository()
        for snapshot in valid + [invalid]:
            await repo.upsert(snapshot)
        ctx = SimpleNamespace(repos=SimpleNamespace(score_repo=repo))

        sessions, qualifying, dates = await self._runner().existing_replay_coverage(
            ctx, as_of, as_of + timedelta(days=1)
        )

        assert sessions == 1
        assert qualifying == 1
        assert dates == {as_of}


def make_snapshot(security_id: str, as_of: date, percentile: int) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"{security_id}:{as_of.isoformat()}",
        security_id=security_id,
        as_of_date=as_of,
        config_version_id="cfg-1",
        cohort_used="tech",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1.0",
        legs={
            LegName.THESIS_LINKAGE: LegResult(
                raw=str(percentile), z=str(percentile), weight="0.5", contribution="0.1", computable=True
            )
        },
        composite=str(percentile),
        percentile=percentile,
        package_fingerprint="fp-1",
        max_knowledge_date=as_of,
    )


def make_daily_bars(security_id: str, start: date, count: int, start_price: int, step: int) -> list[PriceBar]:
    """`count` consecutive daily bars starting at `start`, with `close_adjusted`
    stepping by `step` each session — enough spread to give
    `compute_performance_metrics`'s forward-return/IC math non-degenerate
    (non-zero-variance) input."""

    return [
        make_bar(security_id, start + timedelta(days=i), str(start_price + i * step)) for i in range(count)
    ]


def test_universe_expansion_requires_full_replay_for_missing_security_history() -> None:
    start = date(2025, 1, 1)
    existing = Security(
        id="sec-existing",
        ticker="OLD",
        cik="0000000001",
        name="Existing",
        cohort="platforms",
        filer_profile=FilerProfile.DOMESTIC,
    )
    added = Security(
        id="sec-added",
        ticker="NEW",
        cik="0000000002",
        name="Added",
        cohort="platforms",
        filer_profile=FilerProfile.DOMESTIC,
    )
    score_repo: InMemoryRepository[ScoreSnapshot] = InMemoryRepository()
    for offset in range(MIN_SESSIONS_SCORED):
        snapshot = make_snapshot(
            existing.id,
            start + timedelta(days=offset),
            50,
        ).model_copy(update={"is_backfilled": True})
        asyncio.run(score_repo.upsert(snapshot))
    repos = PipelineRepos(
        document_sink=InMemoryDocumentSink(),
        price_sink=InMemoryPriceSink(),
        fx_sink=InMemoryFxSink(),
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(),
        score_repo=score_repo,
    )
    ctx = PipelineContext(
        universe=Universe(securities=[existing, added]),
        config={},
        as_of_date=start + timedelta(days=MIN_SESSIONS_SCORED),
        user_id="owner",
        repos=repos,
    )
    runner = BootstrapRunner(
        universe=ctx.universe,
        context_factory=lambda _date: ctx,
    )

    incomplete = asyncio.run(
        runner.securities_requiring_full_replay(
            ctx,
            start,
            start + timedelta(days=MIN_SESSIONS_SCORED),
        )
    )

    assert incomplete == {added.id}


def test_universe_expansion_counts_scores_with_partition_queries() -> None:
    start = date(2025, 1, 1)
    securities = [
        Security(
            id="sec-existing",
            ticker="OLD",
            cik="0000000001",
            name="Existing",
            cohort="platforms",
            filer_profile=FilerProfile.DOMESTIC,
        ),
        Security(
            id="sec-added",
            ticker="NEW",
            cik="0000000002",
            name="Added",
            cohort="platforms",
            filer_profile=FilerProfile.DOMESTIC,
        ),
    ]

    class PartitionCountRepository:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        async def raw_query(
            self,
            query: str,
            parameters: list[dict],
            partition_key: str | None = None,
        ) -> list[int]:
            self.calls.append((query, partition_key))
            return [MIN_SESSIONS_SCORED if partition_key == "sec-existing" else 0]

    score_repo = PartitionCountRepository()
    ctx = PipelineContext(
        universe=Universe(securities=securities),
        config={},
        as_of_date=start + timedelta(days=MIN_SESSIONS_SCORED),
        user_id="owner",
        repos=PipelineRepos(
            document_sink=InMemoryDocumentSink(),
            price_sink=InMemoryPriceSink(),
            fx_sink=InMemoryFxSink(),
            fundamental_sink=InMemoryFundamentalSink(),
            blob_sink=InMemoryBlobSink(),
            watermarks=InMemoryWatermarkStore(),
            score_repo=score_repo,
        ),
    )
    runner = BootstrapRunner(
        universe=ctx.universe,
        context_factory=lambda _date: ctx,
    )

    incomplete = asyncio.run(
        runner.securities_requiring_full_replay(
            ctx,
            start,
            start + timedelta(days=MIN_SESSIONS_SCORED),
        )
    )

    assert incomplete == {"sec-added"}
    assert {partition_key for _, partition_key in score_repo.calls} == {
        "sec-existing",
        "sec-added",
    }
    assert all("GROUP BY" not in query for query, _ in score_repo.calls)


class TestComputePerformanceMetricsScoredDatesDerivation:
    """`compute_performance_metrics`'s `scored_dates=None` mode (arc42 §5.8):
    the weekly `job-auspex-performance` job has no replay window of its own,
    so omitting `scored_dates` must derive every distinct `as_of_date`
    already present in `ctx.repos.score_repo` — not silently compute over an
    empty/no-op set — and the derived result must match what an explicit,
    caller-supplied list covering the same dates would produce."""

    def _make_ctx(self, as_of: date, snapshots: list[ScoreSnapshot], bars: list[PriceBar]) -> PipelineContext:
        score_repo: InMemoryRepository[ScoreSnapshot] = InMemoryRepository()
        for snap in snapshots:
            asyncio.run(score_repo.upsert(snap))
        price_sink = InMemoryPriceSink()
        for bar in bars:
            asyncio.run(price_sink.upsert_price_bar(bar))
        repos = PipelineRepos(
            document_sink=InMemoryDocumentSink(),
            price_sink=price_sink,
            fx_sink=InMemoryFxSink(),
            fundamental_sink=InMemoryFundamentalSink(),
            blob_sink=InMemoryBlobSink(),
            watermarks=InMemoryWatermarkStore(),
            score_repo=score_repo,
        )
        return PipelineContext(
            universe=Universe(securities=[]), config={}, as_of_date=as_of, user_id="system", repos=repos
        )

    def _runner(self) -> BootstrapRunner:
        return BootstrapRunner(universe=Universe(securities=[]), context_factory=lambda d: None)

    def test_derives_scored_dates_from_score_repo_and_matches_explicit_list(self):
        as_of = date(2026, 1, 5)
        snapshots = [make_snapshot("sec-a", as_of, 80), make_snapshot("sec-b", as_of, 20)]
        bars = make_daily_bars("sec-a", as_of, 22, 100, 1) + make_daily_bars("sec-b", as_of, 22, 100, -1)
        ctx = self._make_ctx(as_of, snapshots, bars)
        runner = self._runner()

        metrics_derived = asyncio.run(runner.compute_performance_metrics(ctx, scored_dates=None))
        metrics_explicit = asyncio.run(runner.compute_performance_metrics(ctx, scored_dates=[as_of]))

        assert metrics_derived, "omitting scored_dates must not silently compute zero metrics"
        assert [m.model_dump() for m in metrics_derived] == [m.model_dump() for m in metrics_explicit]

    def test_returns_empty_list_when_score_repo_is_empty(self):
        as_of = date(2026, 1, 5)
        ctx = self._make_ctx(as_of, snapshots=[], bars=[])
        runner = self._runner()

        assert asyncio.run(runner.compute_performance_metrics(ctx, scored_dates=None)) == []

    def test_persists_metrics_via_performance_repo_when_provided(self):
        as_of = date(2026, 1, 5)
        snapshots = [make_snapshot("sec-a", as_of, 80), make_snapshot("sec-b", as_of, 20)]
        bars = make_daily_bars("sec-a", as_of, 22, 100, 1) + make_daily_bars("sec-b", as_of, 22, 100, -1)
        ctx = self._make_ctx(as_of, snapshots, bars)
        runner = self._runner()
        performance_repo: InMemoryRepository[PerformanceMetric] = InMemoryRepository()

        metrics = asyncio.run(
            runner.compute_performance_metrics(ctx, scored_dates=None, performance_repo=performance_repo)
        )

        assert metrics
        assert {m.id for m in performance_repo.all()} == {m.id for m in metrics}


class TestPerformanceAttributionPrivacy:
    """Score metrics are shared; recommendation attribution is not (arc42 §5.8).

    Composite/leg IC measure the research and are identical for everybody.
    Suggestion hit rate and disposition outcome describe what one person did
    with their own suggestions, so with several users they must be scoped to
    one user rather than blended into the shared `performance` container.
    """

    def _ctx_with_recommendations(self, as_of: date) -> PipelineContext:
        from auspex.models.enums import Action
        from auspex.models.policy import Recommendation

        score_repo: InMemoryRepository[ScoreSnapshot] = InMemoryRepository()
        snapshots = [make_snapshot("sec-a", as_of, 80), make_snapshot("sec-b", as_of, 20)]
        for snap in snapshots:
            asyncio.run(score_repo.upsert(snap))
        price_sink = InMemoryPriceSink()
        for bar in make_daily_bars("sec-a", as_of, 200, 100, 1) + make_daily_bars(
            "sec-b", as_of, 200, 100, -1
        ):
            asyncio.run(price_sink.upsert_price_bar(bar))

        recommendation_repo: InMemoryRepository[Recommendation] = InMemoryRepository()
        for user_id, security_id in (("user-alice", "sec-a"), ("user-bob", "sec-b")):
            asyncio.run(
                recommendation_repo.upsert(
                    Recommendation(
                        id=f"{user_id}:{security_id}:{as_of.isoformat()}",
                        user_id=user_id,
                        security_id=security_id,
                        as_of_date=as_of,
                        action=Action.BUY,
                        config_version_id="cfg-1",
                    )
                )
            )

        repos = PipelineRepos(
            document_sink=InMemoryDocumentSink(),
            price_sink=price_sink,
            fx_sink=InMemoryFxSink(),
            fundamental_sink=InMemoryFundamentalSink(),
            blob_sink=InMemoryBlobSink(),
            watermarks=InMemoryWatermarkStore(),
            score_repo=score_repo,
            recommendation_repo=recommendation_repo,
        )
        return PipelineContext(
            universe=Universe(securities=[]), config={}, as_of_date=as_of, user_id="system", repos=repos
        )

    def test_score_metrics_are_identical_regardless_of_attribution_scope(self):
        as_of = date(2026, 1, 5)
        runner = BootstrapRunner(universe=Universe(securities=[]), context_factory=lambda d: None)

        unscoped = asyncio.run(
            runner.compute_performance_metrics(self._ctx_with_recommendations(as_of), scored_dates=None)
        )
        scoped = asyncio.run(
            runner.compute_performance_metrics(
                self._ctx_with_recommendations(as_of),
                scored_dates=None,
                attribution_user_id="user-alice",
            )
        )

        def score_metrics(metrics):
            return sorted(
                m.id for m in metrics if not m.metric_type.startswith(("suggestion", "disposition"))
            )

        assert score_metrics(unscoped) == score_metrics(scoped)
        assert score_metrics(scoped), "score metrics must still be produced"

    def test_attribution_scope_excludes_other_users_recommendations(self):
        as_of = date(2026, 1, 5)
        runner = BootstrapRunner(universe=Universe(securities=[]), context_factory=lambda d: None)

        alice = asyncio.run(
            runner.compute_performance_metrics(
                self._ctx_with_recommendations(as_of),
                scored_dates=None,
                attribution_user_id="user-alice",
            )
        )
        nobody = asyncio.run(
            runner.compute_performance_metrics(
                self._ctx_with_recommendations(as_of),
                scored_dates=None,
                attribution_user_id="user-nobody",
            )
        )

        def attribution(metrics):
            return [m for m in metrics if m.metric_type.startswith(("suggestion", "disposition"))]

        # A user with no recommendations of their own yields no attribution.
        assert attribution(nobody) == []
        # Alice's own suggestions still produce her attribution metrics.
        assert attribution(alice)
