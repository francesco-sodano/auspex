"""Unit tests for cohort normalisation and the fallback ladder (arc42 §5.5).

```
len(cohort) >= 12 -> scope=cohort,  confidence=HIGH
len(parent) >= 8  -> scope=parent,  confidence=MEDIUM
else              -> scope=universe, confidence=LOW
```
"""

from __future__ import annotations

from decimal import Decimal

from auspex.models.enums import CohortConfidence
from auspex.scoring.normalize import (
    assign_cohort_scope,
    blended_percentile_rank,
    blended_zscore,
    clip,
    exponential_decay,
    mean_std,
    percentile_rank,
    percentile_rank_fraction,
    shrinkage_lambda,
    shrinkage_tier_weights,
    winsorise,
    zscore,
)


class TestCohortFallbackLadder:
    def test_cohort_scope_used_when_at_least_12_members(self):
        scope = assign_cohort_scope(
            cohort_name="semi-compute",
            cohort_member_ids=[f"s{i}" for i in range(12)],
            parent_name="semiconductors",
            parent_member_ids=[f"s{i}" for i in range(40)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.scope == "semi-compute"
        assert scope.confidence == CohortConfidence.HIGH
        assert len(scope.member_ids) == 12

    def test_falls_back_to_parent_when_cohort_below_12_but_parent_at_least_8(self):
        scope = assign_cohort_scope(
            cohort_name="thin-cohort",
            cohort_member_ids=[f"s{i}" for i in range(5)],
            parent_name="semiconductors",
            parent_member_ids=[f"s{i}" for i in range(9)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.scope == "semiconductors"
        assert scope.confidence == CohortConfidence.MEDIUM

    def test_falls_back_to_universe_when_both_thin(self):
        scope = assign_cohort_scope(
            cohort_name="thin-cohort",
            cohort_member_ids=[f"s{i}" for i in range(3)],
            parent_name="thin-parent",
            parent_member_ids=[f"s{i}" for i in range(4)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.scope == "universe"
        assert scope.confidence == CohortConfidence.LOW
        assert len(scope.member_ids) == 92

    def test_boundary_exactly_12_is_high(self):
        scope = assign_cohort_scope(
            cohort_name="c",
            cohort_member_ids=[f"s{i}" for i in range(12)],
            parent_name="p",
            parent_member_ids=[f"s{i}" for i in range(50)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.confidence == CohortConfidence.HIGH

    def test_boundary_11_falls_back(self):
        scope = assign_cohort_scope(
            cohort_name="c",
            cohort_member_ids=[f"s{i}" for i in range(11)],
            parent_name="p",
            parent_member_ids=[f"s{i}" for i in range(8)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.scope == "p"
        assert scope.confidence == CohortConfidence.MEDIUM

    def test_boundary_exactly_8_parent_is_medium(self):
        scope = assign_cohort_scope(
            cohort_name="c",
            cohort_member_ids=[f"s{i}" for i in range(3)],
            parent_name="p",
            parent_member_ids=[f"s{i}" for i in range(8)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.confidence == CohortConfidence.MEDIUM

    def test_boundary_7_parent_falls_back_to_universe(self):
        scope = assign_cohort_scope(
            cohort_name="c",
            cohort_member_ids=[f"s{i}" for i in range(3)],
            parent_name="p",
            parent_member_ids=[f"s{i}" for i in range(7)],
            universe_member_ids=[f"s{i}" for i in range(92)],
        )
        assert scope.scope == "universe"
        assert scope.confidence == CohortConfidence.LOW


class TestShrinkage:
    """Continuous shrinkage replaces the cliff at the cohort-size threshold.

    The old ladder switched a security's entire reference population the moment
    a peer was added or removed at n == 12, so a single membership change could
    move a z-score (and therefore a recommendation) discontinuously. Weighting
    the cohort, parent and universe cross-sections by ``n / (n + k)`` makes the
    same transition continuous and explainable: nothing snaps, the cohort simply
    earns more of the weight as it grows.
    """

    def test_lambda_is_zero_for_empty_cohort(self):
        assert shrinkage_lambda(0) == Decimal(0)
        assert shrinkage_lambda(-3) == Decimal(0)

    def test_lambda_increases_with_membership(self):
        lambdas = [shrinkage_lambda(n) for n in range(0, 40)]
        assert lambdas == sorted(lambdas)
        assert all(Decimal(0) <= value <= Decimal(1) for value in lambdas)

    def test_lambda_approaches_one_for_large_cohorts(self):
        assert shrinkage_lambda(1000) > Decimal("0.98")

    def test_lambda_is_continuous_across_the_old_cliff(self):
        # The legacy ladder jumped from "parent" to "cohort" between 11 and 12.
        step = shrinkage_lambda(12) - shrinkage_lambda(11)
        assert step < Decimal("0.03")

    def test_tier_weights_always_sum_to_one(self):
        for n_cohort in (0, 1, 5, 12, 40):
            for n_parent in (0, 3, 8, 25):
                weights = shrinkage_tier_weights(shrinkage_lambda(n_cohort), shrinkage_lambda(n_parent))
                assert sum(weights) == Decimal(1)
                assert all(weight >= Decimal(0) for weight in weights)

    def test_empty_cohort_defers_entirely_to_broader_scopes(self):
        cohort_weight, _, _ = shrinkage_tier_weights(Decimal(0), Decimal("0.5"))
        assert cohort_weight == Decimal(0)

    def test_scope_label_still_matches_the_documented_ladder(self):
        scope = assign_cohort_scope(
            cohort_name="c",
            cohort_member_ids=[f"s{i}" for i in range(12)],
            parent_name="p",
            parent_member_ids=[f"s{i}" for i in range(30)],
            universe_member_ids=[f"s{i}" for i in range(90)],
        )
        assert scope.scope == "c"
        assert scope.lambda_cohort >= Decimal("0.5")


class TestBlendedStatistics:
    def test_blend_falls_back_to_parent_when_cohort_is_degenerate(self):
        z = blended_zscore(
            Decimal(5),
            cohort_values=[Decimal(5)],  # single observation: no dispersion
            parent_values=[Decimal(i) for i in range(1, 10)],
            lambda_cohort=Decimal("0.2"),
            lambda_parent=Decimal("0.8"),
        )
        assert z is not None

    def test_blend_is_none_when_no_tier_is_usable(self):
        assert blended_zscore(Decimal(5), cohort_values=[Decimal(5), Decimal(5)]) is None

    def test_blend_matches_single_tier_when_only_cohort_supplied(self):
        values = [Decimal(1), Decimal(2), Decimal(3), Decimal(4), Decimal(5)]
        mean, std = mean_std(values)
        assert std is not None
        blended = blended_zscore(Decimal(5), cohort_values=values)
        assert blended == zscore(Decimal(5), mean, std)

    def test_blended_percentile_respects_the_midpoint_convention(self):
        values = [Decimal(i) for i in range(1, 11)]
        assert blended_percentile_rank(Decimal(10), cohort_population=values) == 95




    def test_simple_population(self):
        mean, std = mean_std(
            [Decimal(2), Decimal(4), Decimal(4), Decimal(4), Decimal(5), Decimal(5), Decimal(7), Decimal(9)]
        )
        assert mean == Decimal(5)
        assert std == Decimal(2)

    def test_constant_population_has_zero_std(self):
        mean, std = mean_std([Decimal(3), Decimal(3), Decimal(3)])
        assert mean == Decimal(3)
        assert std == Decimal(0)


class TestZscore:
    def test_zscore_basic(self):
        z = zscore(Decimal(7), Decimal(5), Decimal(2))
        assert z == Decimal("1")

    def test_zscore_none_when_std_zero(self):
        assert zscore(Decimal(5), Decimal(5), Decimal(0)) is None


class TestWinsorise:
    def test_within_bounds_unchanged(self):
        assert winsorise(Decimal("1.0")) == Decimal("1.0")

    def test_clipped_at_positive_sigma(self):
        assert winsorise(Decimal("3.0"), Decimal("2.5")) == Decimal("2.5")

    def test_clipped_at_negative_sigma(self):
        assert winsorise(Decimal("-3.0"), Decimal("2.5")) == Decimal("-2.5")


class TestPercentileRank:
    """Midpoint (tie-aware) percentile ranks.

    The rank of a value is ``(strictly_below + 0.5 * ties) / n``. Counting the
    value against itself is what used to hand the best member of a three-name
    cohort a flat 100th percentile, which then read as "top of the market"
    everywhere downstream. The midpoint convention keeps the endpoints inside
    the open interval, so a small cohort can no longer manufacture extremes.
    """

    def test_median_value(self):
        population = [Decimal(i) for i in range(1, 11)]  # 1..10
        # 4 strictly below, 1 tie -> (4 + 0.5) / 10 = 45%.
        assert percentile_rank(Decimal(5), population) == 45

    def test_top_value_is_below_100(self):
        population = [Decimal(i) for i in range(1, 11)]
        assert percentile_rank(Decimal(10), population) == 95

    def test_bottom_value_is_above_zero(self):
        population = [Decimal(i) for i in range(1, 11)]
        assert percentile_rank(Decimal(1), population) == 5

    def test_empty_population_returns_zero(self):
        assert percentile_rank(Decimal(1), []) == 0

    def test_small_cohort_endpoints_are_not_inflated(self):
        population = [Decimal(1), Decimal(2), Decimal(3)]
        assert percentile_rank(Decimal(3), population) == 83
        assert percentile_rank(Decimal(1), population) == 17

    def test_ties_share_the_same_rank(self):
        population = [Decimal(1), Decimal(5), Decimal(5), Decimal(9)]
        # Both tied members: (1 + 0.5 * 2) / 4 = 50%.
        assert percentile_rank(Decimal(5), population) == 50

    def test_all_tied_population_is_the_midpoint(self):
        population = [Decimal(4)] * 7
        assert percentile_rank(Decimal(4), population) == 50

    def test_fraction_is_symmetric_about_the_midpoint(self):
        population = [Decimal(i) for i in range(1, 8)]
        low = percentile_rank_fraction(Decimal(1), population)
        high = percentile_rank_fraction(Decimal(7), population)
        assert low is not None and high is not None
        assert low + high == Decimal(1)

    def test_fraction_is_none_for_empty_population(self):
        assert percentile_rank_fraction(Decimal(1), []) is None

    def test_rank_is_monotone_non_decreasing(self):
        population = [Decimal(i) for i in range(1, 21)]
        ranks = [percentile_rank(Decimal(i), population) for i in range(1, 21)]
        assert ranks == sorted(ranks)
        assert all(0 < rank < 100 for rank in ranks)


class TestClipAndDecay:
    def test_clip_within_bounds(self):
        assert clip(Decimal("0.5"), Decimal(0), Decimal(1)) == Decimal("0.5")

    def test_clip_below_floor(self):
        assert clip(Decimal("-1"), Decimal(0), Decimal(1)) == Decimal(0)

    def test_clip_above_ceiling(self):
        assert clip(Decimal("2"), Decimal(0), Decimal(1)) == Decimal(1)

    def test_decay_at_zero_age_is_one(self):
        assert exponential_decay(0, Decimal(90)) == Decimal(1)

    def test_decay_at_time_constant_is_exp_neg_one(self):
        # arc42 §5.5 specifies exp(-age_days/half_life) literally (a time-constant
        # decay, not the "true" half-life formula) — at age == half_life this is exp(-1).
        decay = exponential_decay(90, Decimal(90))
        assert abs(decay - Decimal("0.367879441")) < Decimal("0.0001")

    def test_decay_decreases_with_age(self):
        assert exponential_decay(180, Decimal(90)) < exponential_decay(90, Decimal(90))
