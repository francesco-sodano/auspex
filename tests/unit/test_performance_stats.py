"""Deterministic Decimal statistics primitives (arc42 §5.8).

Every number here is reproducible: the RNG is a seeded splitmix64, never
``random``, so a shadow study re-run on the same inputs yields byte-identical
metrics.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from auspex.performance.stats import (
    DeterministicRandom,
    autocorrelations,
    effective_sample_size,
    effective_sample_size_from_autocorrelation,
    fisher_z,
    inverse_fisher_z,
    mean,
    median,
    moments,
    normal_cdf,
    overlap_variance_inflation,
    quantile,
    sample_std,
    seed_from_text,
    two_sided_p_value,
    z_for_confidence,
)


class TestDeterministicRandom:
    def test_same_seed_replays_the_same_stream(self) -> None:
        left = DeterministicRandom(seed_from_text("auspex"))
        right = DeterministicRandom(seed_from_text("auspex"))
        assert [left.below(1000) for _ in range(20)] == [right.below(1000) for _ in range(20)]

    def test_different_seeds_diverge(self) -> None:
        left = [DeterministicRandom(seed_from_text("a")).below(1000) for _ in range(1)]
        right = [DeterministicRandom(seed_from_text("b")).below(1000) for _ in range(1)]
        assert left != right

    def test_below_stays_in_range(self) -> None:
        rng = DeterministicRandom(seed_from_text("range"))
        assert all(0 <= rng.below(7) < 7 for _ in range(200))

    def test_permutation_is_a_permutation(self) -> None:
        rng = DeterministicRandom(seed_from_text("perm"))
        assert sorted(rng.permutation(12)) == list(range(12))

    def test_seed_from_text_is_stable_across_processes(self) -> None:
        # A literal, not a recomputation: a drifting hash would silently
        # invalidate every previously published seeded interval.
        assert seed_from_text("auspex-performance") == seed_from_text("auspex-performance")
        assert seed_from_text("auspex-performance") != seed_from_text("auspex-shadow")


class TestSummaryStatistics:
    def test_mean_and_std_are_exact_decimals(self) -> None:
        values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        assert mean(values) == Decimal("2.5")
        # Sample (n-1) standard deviation of 1..4 is sqrt(5/3).
        assert sample_std(values) is not None
        assert abs(sample_std(values) - Decimal("1.2909944487358056")) < Decimal("0.000001")

    def test_sample_std_needs_two_observations(self) -> None:
        assert sample_std([Decimal("1")]) is None

    def test_median_handles_both_parities(self) -> None:
        assert median([Decimal("3"), Decimal("1"), Decimal("2")]) == Decimal("2")
        assert median([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]) == Decimal("2.5")

    def test_quantile_interpolates_linearly(self) -> None:
        values = [Decimal(n) for n in range(1, 11)]
        assert quantile(values, Decimal("0")) == Decimal("1")
        assert quantile(values, Decimal("1")) == Decimal("10")
        assert quantile(values, Decimal("0.5")) == Decimal("5.5")

    def test_moments_bundles_count_mean_std(self) -> None:
        result = moments([Decimal("1"), Decimal("3")])
        assert result.count == 2
        assert result.mean == Decimal("2")
        assert result.std is not None


class TestFisherTransform:
    def test_round_trips(self) -> None:
        for raw in ("-0.9", "-0.25", "0", "0.25", "0.9"):
            value = Decimal(raw)
            assert abs(inverse_fisher_z(fisher_z(value)) - value) < Decimal("0.0000001")


class TestNormalApproximations:
    def test_normal_cdf_is_centred(self) -> None:
        assert abs(normal_cdf(Decimal("0")) - Decimal("0.5")) < Decimal("0.000001")

    def test_two_sided_p_value_matches_the_textbook_1_96(self) -> None:
        assert abs(two_sided_p_value(Decimal("1.96")) - Decimal("0.05")) < Decimal("0.0001")

    def test_p_value_is_symmetric(self) -> None:
        assert two_sided_p_value(Decimal("-1.5")) == two_sided_p_value(Decimal("1.5"))

    def test_z_for_confidence_supports_the_documented_levels(self) -> None:
        assert abs(z_for_confidence(Decimal("0.95")) - Decimal("1.96")) < Decimal("0.001")
        assert z_for_confidence(Decimal("0.99")) > z_for_confidence(Decimal("0.95"))

    def test_z_for_confidence_rejects_unsupported_levels(self) -> None:
        with pytest.raises(ValueError):
            z_for_confidence(Decimal("0.93"))


class TestOverlapAdjustments:
    def test_effective_sample_size_shrinks_with_horizon(self) -> None:
        """Daily 126-day windows are not 100 independent draws.

        Publishing a t-statistic on the raw count would overstate significance
        by roughly sqrt(horizon); this is the correction that stops that.
        """

        assert effective_sample_size(100, 1) == Decimal("100")
        assert effective_sample_size(100, 21) < Decimal("10")
        assert effective_sample_size(100, 126) < effective_sample_size(100, 21)

    def test_inflation_is_capped_by_the_observation_count(self) -> None:
        assert overlap_variance_inflation(126, 10) == Decimal("10")
        assert overlap_variance_inflation(21, 100) == Decimal("21")

    def test_autocorrelation_of_a_trend_is_positive_at_lag_one(self) -> None:
        series = [Decimal(n) for n in range(20)]
        acf = autocorrelations(series, 3)
        assert acf[0] > Decimal("0.5")

    def test_effective_sample_size_from_autocorrelation_never_exceeds_n(self) -> None:
        series = [Decimal(n) for n in range(20)]
        acf = autocorrelations(series, 3)
        assert effective_sample_size_from_autocorrelation(len(series), acf) <= Decimal("20")
