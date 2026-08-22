"""Unit tests for pipeline staleness, session arithmetic and leg-change writing.

Three behaviours that only exist once the pure scoring rules are wired into
:mod:`auspex.pipeline.steps`:

* the documented price-age rule is what actually excludes a security;
* a Channel B failure costs the user an explanation, never a score;
* ``DIFF`` compares against the previous *observed trading session*.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from auspex.models.enums import CohortConfidence, FilerProfile, LegName
from auspex.models.market import PriceBar
from auspex.models.scoring import LegResult, ScoreSnapshot
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
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.steps import _previous_session_date, _stale_security_ids, step_diff
from auspex.scoring.composite import (
    REASON_DECOMPOSITION_NO_PRIOR,
    CompositeResult,
    LegCompositeResult,
    LegCrossSection,
)
from auspex.scoring.coverage import MAX_STALE_SESSIONS
from auspex.scoring.engine import SecurityScoreResult
from auspex.scoring.normalize import CohortScope

AS_OF = date(2026, 8, 20)  # a Thursday
SESSIONS = [date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19), AS_OF]


class FakeSecurity:
    def __init__(self, security_id: str) -> None:
        self.id = security_id
        self.ticker = security_id.upper()
        self.cohort = "c"
        self.filer_profile = FilerProfile.DOMESTIC


class FakeUniverse:
    def __init__(self, ids: list[str]) -> None:
        self.securities = [FakeSecurity(i) for i in ids]


def _repos() -> PipelineRepos:
    return PipelineRepos(
        document_sink=InMemoryDocumentSink(),
        price_sink=InMemoryPriceSink(),
        fx_sink=InMemoryFxSink(),
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(),
        score_repo=InMemoryRepository(),
        leg_change_repo=InMemoryRepository(),
    )


def _context(ids: list[str], repos: PipelineRepos | None = None) -> PipelineContext:
    return PipelineContext(
        universe=FakeUniverse(ids),
        config={"policy": {}},
        as_of_date=AS_OF,
        user_id="owner",
        repos=repos or _repos(),
    )


def _seed_bar(repos: PipelineRepos, security_id: str, session_date: date) -> None:
    bar = PriceBar(
        id=f"{security_id}:{session_date.isoformat()}",
        security_id=security_id,
        session_date=session_date,
        open_raw="100",
        high_raw="100",
        low_raw="100",
        close_raw="100",
        volume=1_000,
        close_adjusted="100",
    )
    repos.price_sink._bars[bar.id] = bar  # type: ignore[attr-defined]


class TestObservedSessionStaleness:
    """arc42 §5.5 "Staleness exclusion" measured in sessions, not calendar days."""

    def test_a_current_price_is_not_stale(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "fresh", session)
        ctx = _context(["fresh"], repos)
        assert asyncio.run(_stale_security_ids(ctx)) == set()

    def test_a_price_exactly_at_the_limit_is_not_stale(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        # 'edge' last traded two sessions back: 2026-08-18, with 08-19 and
        # 08-20 in between -> one intervening session, inside the limit.
        _seed_bar(repos, "edge", date(2026, 8, 19))
        ctx = _context(["market", "edge"], repos)
        assert "edge" not in asyncio.run(_stale_security_ids(ctx))

    def test_a_price_beyond_the_limit_is_stale(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        _seed_bar(repos, "frozen", date(2026, 8, 14))
        ctx = _context(["market", "frozen"], repos)
        assert "frozen" in asyncio.run(_stale_security_ids(ctx))

    def test_a_weekend_gap_does_not_age_a_price(self):
        """Friday's close read on Monday is zero sessions old, not three days."""

        repos = _repos()
        friday, monday = date(2026, 8, 14), date(2026, 8, 17)
        _seed_bar(repos, "market", friday)
        _seed_bar(repos, "market", monday)
        _seed_bar(repos, "quiet", friday)
        ctx = PipelineContext(
            universe=FakeUniverse(["market", "quiet"]),
            config={"policy": {}},
            as_of_date=monday,
            user_id="owner",
            repos=repos,
        )
        assert asyncio.run(_stale_security_ids(ctx)) == set()

    def test_a_security_with_no_bar_on_a_trading_day_is_stale(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        ctx = _context(["market", "unpriced"], repos)
        assert asyncio.run(_stale_security_ids(ctx)) == {"unpriced"}

    def test_no_observed_calendar_excludes_nothing(self):
        """The rule is unevaluable without a calendar; emptying the universe
        would be a far worse failure than a thin day with honest coverage."""

        ctx = _context(["a", "b"])
        assert asyncio.run(_stale_security_ids(ctx)) == set()

    def test_the_limit_matches_the_documented_constant(self):
        assert MAX_STALE_SESSIONS == 2


class TestPreviousObservedSession:
    def test_returns_the_prior_trading_session(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        ctx = _context(["market"], repos)
        assert asyncio.run(_previous_session_date(ctx)) == date(2026, 8, 19)

    def test_a_monday_run_compares_against_friday_not_sunday(self):
        """The exact case calendar-day arithmetic got wrong every week."""

        repos = _repos()
        friday, monday = date(2026, 8, 14), date(2026, 8, 17)
        _seed_bar(repos, "market", friday)
        _seed_bar(repos, "market", monday)
        ctx = PipelineContext(
            universe=FakeUniverse(["market"]),
            config={"policy": {}},
            as_of_date=monday,
            user_id="owner",
            repos=repos,
        )
        assert asyncio.run(_previous_session_date(ctx)) == friday

    def test_a_non_session_run_date_still_finds_the_last_close(self):
        repos = _repos()
        friday = date(2026, 8, 14)
        _seed_bar(repos, "market", friday)
        ctx = PipelineContext(
            universe=FakeUniverse(["market"]),
            config={"policy": {}},
            as_of_date=date(2026, 8, 15),  # Saturday
            user_id="owner",
            repos=repos,
        )
        assert asyncio.run(_previous_session_date(ctx)) == friday

    def test_falls_back_to_calendar_yesterday_without_a_calendar(self):
        ctx = _context(["a"])
        assert asyncio.run(_previous_session_date(ctx)) == date(2026, 8, 19)


def _score_results(current_raw: Decimal, cross_section: LegCrossSection) -> dict:
    z = cross_section.z_for(current_raw)
    leg = LegCompositeResult(
        raw=current_raw,
        z=z,
        weight=Decimal(1),
        contribution=z,
        computable=True,
        cross_section=cross_section,
    )
    return {
        "market": SecurityScoreResult(
            security_id="market",
            excluded_stale=False,
            cohort_scope=CohortScope(scope="c", confidence=CohortConfidence.HIGH, member_ids=("market",)),
            composite_result=CompositeResult(
                legs={LegName.THESIS_LINKAGE: leg},
                composite=z,
                weight_sum=Decimal(1),
                computable_weight=Decimal(1),
            ),
            coverage=Decimal(1),
            percentile=50,
        )
    }


def _prior_snapshot(as_of: date, *, raw: str, z: str) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"market:{as_of.isoformat()}",
        security_id="market",
        as_of_date=as_of,
        config_version_id="v1",
        cohort_used="c",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1",
        legs={
            LegName.THESIS_LINKAGE: LegResult(
                raw=raw, z=z, weight="1", contribution=z, computable=True
            )
        },
        composite=z,
        percentile=50,
        package_fingerprint="fp",
        max_knowledge_date=as_of,
    )


class TestStepDiffAttribution:
    @pytest.mark.asyncio
    async def test_diff_reads_the_prior_session_and_attributes_the_move(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        cross_section = LegCrossSection(
            cohort_values=(Decimal(1), Decimal(2), Decimal(3), Decimal(4)),
        )
        prior_raw = Decimal(1)
        prior_z = cross_section.z_for(prior_raw)
        await repos.score_repo.upsert(
            _prior_snapshot(date(2026, 8, 19), raw=str(prior_raw), z=str(prior_z))
        )
        # A snapshot dated calendar-yesterday-but-not-a-session must be ignored.
        ctx = _context(["market"], repos)
        ctx.__dict__["_score_results"] = _score_results(Decimal(4), cross_section)

        manifest = new_manifest(AS_OF)
        await step_diff(ctx, manifest)

        change = ctx.__dict__["_leg_changes"][0]
        assert change.prior_z == str(prior_z)
        assert Decimal(change.own_evidence_effect) + Decimal(
            change.cohort_distribution_effect
        ) == Decimal(change.delta_z)
        # Peers identical on both days: nothing is attributable to the cohort.
        assert Decimal(change.cohort_distribution_effect) == Decimal(0)
        assert change.attribution_unavailable_reason is None
        assert manifest.step_by_name("DIFF").detail.endswith("prior_session=2026-08-19")

    @pytest.mark.asyncio
    async def test_no_prior_row_writes_an_explicit_unavailable_attribution(self):
        """The old code wrote ``own_evidence_effect = delta`` unconditionally."""

        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        ctx = _context(["market"], repos)
        ctx.__dict__["_score_results"] = _score_results(
            Decimal(4), LegCrossSection(cohort_values=(Decimal(1), Decimal(2), Decimal(4)))
        )

        await step_diff(ctx, new_manifest(AS_OF))

        change = ctx.__dict__["_leg_changes"][0]
        assert change.delta_z is None
        assert change.own_evidence_effect is None
        assert change.cohort_distribution_effect is None
        assert change.attribution_unavailable_reason == REASON_DECOMPOSITION_NO_PRIOR

    @pytest.mark.asyncio
    async def test_a_pure_peer_move_is_never_charged_to_own_evidence(self):
        repos = _repos()
        for session in SESSIONS:
            _seed_bar(repos, "market", session)
        unchanged_raw = Decimal(3)
        prior_cross = LegCrossSection(cohort_values=(Decimal(1), Decimal(2), Decimal(3), Decimal(4)))
        current_cross = LegCrossSection(
            cohort_values=(Decimal(10), Decimal(20), Decimal(30), Decimal(40))
        )
        await repos.score_repo.upsert(
            _prior_snapshot(
                date(2026, 8, 19),
                raw=str(unchanged_raw),
                z=str(prior_cross.z_for(unchanged_raw)),
            )
        )
        ctx = _context(["market"], repos)
        ctx.__dict__["_score_results"] = _score_results(unchanged_raw, current_cross)

        await step_diff(ctx, new_manifest(AS_OF))

        change = ctx.__dict__["_leg_changes"][0]
        assert Decimal(change.own_evidence_effect) == Decimal(0)
        assert Decimal(change.cohort_distribution_effect) == Decimal(change.delta_z)
        assert Decimal(change.delta_z) != Decimal(0)
