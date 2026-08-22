"""Unit tests for the scoring orchestrator (``auspex.scoring.engine``).

Covers the two things ``score_universe`` is responsible for that no other
module can assert: that the composite *percentile* is shrinkage-blended across
the same three tiers the leg z-scores use, and that a stale security leaves
both the output and every peer's cross-section.
"""

from __future__ import annotations

from decimal import Decimal

from auspex.models.enums import FilerProfile, LegName
from auspex.scoring.engine import SecurityScoringInput, score_universe
from auspex.scoring.normalize import (
    assign_cohort_scope,
    blended_percentile_rank,
    percentile_rank,
)

LEG = LegName.THESIS_LINKAGE
WEIGHTS = {FilerProfile.DOMESTIC: {LEG: Decimal("1")}}

COHORT_IDS = [f"c{i}" for i in range(3)]
SIBLING_IDS = [f"s{i}" for i in range(9)]
OTHER_IDS = [f"o{i}" for i in range(20)]
PARENT_IDS = COHORT_IDS + SIBLING_IDS
ALL_IDS = PARENT_IDS + OTHER_IDS


def _raw(security_id: str) -> Decimal:
    """A deterministic spread that makes ``c0`` top of its own thin cohort
    while sitting only a little above the middle of the whole universe."""

    if security_id in COHORT_IDS:
        return {"c0": Decimal("0.60"), "c1": Decimal("0.20"), "c2": Decimal("0.10")}[security_id]
    if security_id in SIBLING_IDS:
        return Decimal("0.50") + Decimal(SIBLING_IDS.index(security_id)) / Decimal(100)
    return Decimal("0.80") + Decimal(OTHER_IDS.index(security_id)) / Decimal(100)


def _scopes(stale: set[str] | None = None):
    stale = stale or set()

    def live(ids: list[str]) -> list[str]:
        return [i for i in ids if i not in stale]

    thin = assign_cohort_scope(
        cohort_name="thin",
        cohort_member_ids=live(COHORT_IDS),
        parent_name="parent",
        parent_member_ids=live(PARENT_IDS),
        universe_member_ids=live(ALL_IDS),
    )
    sibling = assign_cohort_scope(
        cohort_name="sibling",
        cohort_member_ids=live(SIBLING_IDS),
        parent_name="parent",
        parent_member_ids=live(PARENT_IDS),
        universe_member_ids=live(ALL_IDS),
    )
    other = assign_cohort_scope(
        cohort_name="other",
        cohort_member_ids=live(OTHER_IDS),
        parent_name="other-parent",
        parent_member_ids=live(OTHER_IDS),
        universe_member_ids=live(ALL_IDS),
    )
    by_security = {}
    for sid in ALL_IDS:
        if sid in stale:
            continue
        by_security[sid] = thin if sid in COHORT_IDS else (sibling if sid in SIBLING_IDS else other)
    return thin, by_security


def _inputs(stale: set[str] | None = None) -> list[SecurityScoringInput]:
    stale = stale or set()
    return [
        SecurityScoringInput(
            security_id=sid,
            filer_profile=FilerProfile.DOMESTIC,
            is_stale=sid in stale,
            leg_raw={LEG: _raw(sid)},
        )
        for sid in ALL_IDS
    ]


class TestBlendedCompositePercentile:
    """The composite percentile must not step when a cohort crosses a threshold.

    Leg z-scores were already blended across cohort/parent/universe, but the
    percentile a user actually reads was ranked inside the reported scope's
    population alone. That reintroduced the fallback-ladder cliff one level up:
    a cohort gaining a twelfth member re-based the whole rank at once.
    """

    def test_percentile_is_the_shrinkage_blend_of_the_three_tier_ranks(self):
        thin, scope_by_security = _scopes()
        results = score_universe(_inputs(), WEIGHTS, scope_by_security)

        composites = {
            sid: res.composite_result.composite
            for sid, res in results.items()
            if res.composite_result is not None
        }
        expected = blended_percentile_rank(
            composites["c0"],
            cohort_population=[composites[i] for i in COHORT_IDS],
            parent_population=[composites[i] for i in PARENT_IDS],
            universe_population=[composites[i] for i in ALL_IDS],
            lambda_cohort=thin.lambda_cohort,
            lambda_parent=thin.lambda_parent,
        )
        assert results["c0"].percentile == expected

    def test_a_thin_cohort_can_no_longer_manufacture_a_top_of_market_rank(self):
        """``c0`` leads two peers but is unremarkable against the universe."""

        _, scope_by_security = _scopes()
        results = score_universe(_inputs(), WEIGHTS, scope_by_security)
        composites = {
            sid: res.composite_result.composite
            for sid, res in results.items()
            if res.composite_result is not None
        }

        cohort_only = percentile_rank(composites["c0"], [composites[i] for i in COHORT_IDS])
        assert cohort_only > 80  # top of its own three-name cohort
        assert results["c0"].percentile is not None
        assert results["c0"].percentile < cohort_only  # the wider tiers pull it back

    def test_a_large_cohort_keeps_essentially_its_own_ranking(self):
        """With ``lambda_cohort`` near 1 the blend is the cohort rank."""

        big = [f"b{i}" for i in range(60)]
        scope = assign_cohort_scope(
            cohort_name="big",
            cohort_member_ids=big,
            parent_name="parent",
            parent_member_ids=big,
            universe_member_ids=big,
        )
        inputs = [
            SecurityScoringInput(
                security_id=sid,
                filer_profile=FilerProfile.DOMESTIC,
                is_stale=False,
                leg_raw={LEG: Decimal(index)},
            )
            for index, sid in enumerate(big)
        ]
        results = score_universe(inputs, WEIGHTS, dict.fromkeys(big, scope))
        composites = [results[sid].composite_result.composite for sid in big]
        assert results["b59"].percentile == percentile_rank(composites[-1], composites)

    def test_scopes_without_explicit_tiers_rank_within_their_reported_label(self):
        """Hand-built scopes (replay fixtures) keep their historical behaviour."""

        from auspex.models.enums import CohortConfidence
        from auspex.scoring.normalize import CohortScope

        ids = [f"x{i}" for i in range(5)]
        scope = CohortScope(
            scope="legacy",
            confidence=CohortConfidence.HIGH,
            member_ids=tuple(ids),
        )
        inputs = [
            SecurityScoringInput(
                security_id=sid,
                filer_profile=FilerProfile.DOMESTIC,
                is_stale=False,
                leg_raw={LEG: Decimal(index)},
            )
            for index, sid in enumerate(ids)
        ]
        results = score_universe(inputs, WEIGHTS, dict.fromkeys(ids, scope))
        composites = [results[sid].composite_result.composite for sid in ids]
        assert results["x4"].percentile == percentile_rank(composites[-1], composites)


class TestStaleExclusion:
    def test_a_stale_security_is_reported_but_not_scored(self):
        _, scope_by_security = _scopes(stale={"c1"})
        results = score_universe(_inputs(stale={"c1"}), WEIGHTS, scope_by_security)

        stale = results["c1"]
        assert stale.excluded_stale is True
        assert stale.composite_result is None
        assert stale.percentile is None
        assert stale.coverage == Decimal(0)

    def test_a_stale_security_leaves_its_peers_cross_section(self):
        """A frozen price must not keep voting in its cohort's statistics."""

        _, with_all = _scopes()
        _, without_c1 = _scopes(stale={"c1"})

        full = score_universe(_inputs(), WEIGHTS, with_all)
        reduced = score_universe(_inputs(stale={"c1"}), WEIGHTS, without_c1)

        assert full["c0"].composite_result.composite != reduced["c0"].composite_result.composite
