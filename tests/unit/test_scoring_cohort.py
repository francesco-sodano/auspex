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
    clip,
    exponential_decay,
    mean_std,
    percentile_rank,
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


class TestMeanStd:
    def test_empty_returns_none(self):
        assert mean_std([]) == (None, None)

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
    def test_median_value(self):
        population = [Decimal(i) for i in range(1, 11)]  # 1..10
        pct = percentile_rank(Decimal(5), population)
        assert pct == 50

    def test_top_value_is_100(self):
        population = [Decimal(i) for i in range(1, 11)]
        assert percentile_rank(Decimal(10), population) == 100

    def test_bottom_value_is_10(self):
        population = [Decimal(i) for i in range(1, 11)]
        assert percentile_rank(Decimal(1), population) == 10

    def test_empty_population_returns_zero(self):
        assert percentile_rank(Decimal(1), []) == 0


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
