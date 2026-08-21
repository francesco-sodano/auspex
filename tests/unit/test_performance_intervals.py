"""Overlapping-horizon confidence intervals and IC distributions (arc42 §5.8).

Forward returns at 21/63/126 days are measured on daily overlapping windows, so
the naive standard error is far too small. These tests pin the two corrections
(Newey-West HAC and a seeded moving-block bootstrap) and the distribution
summary that reports ICIR alongside an effective sample size.
"""

from __future__ import annotations

from decimal import Decimal

from auspex.performance.distribution import ic_distribution, information_ratio
from auspex.performance.intervals import (
    block_bootstrap_interval,
    moving_block_bootstrap_means,
    newey_west_interval,
    newey_west_standard_error,
)
from auspex.performance.stats import seed_from_text

# A mildly persistent IC series: positive on average, but autocorrelated the
# way overlapping windows always are.
PERSISTENT = [
    Decimal(raw)
    for raw in (
        "0.02", "0.03", "0.05", "0.04", "0.06", "0.05", "0.03", "0.01",
        "-0.01", "0.00", "0.02", "0.04", "0.05", "0.06", "0.04", "0.03",
        "0.02", "0.01", "0.03", "0.05", "0.06", "0.07", "0.05", "0.04",
    )
]


class TestNeweyWest:
    def test_standard_error_needs_two_observations(self) -> None:
        assert newey_west_standard_error([Decimal("0.1")], lag=5) is None

    def test_degenerate_variance_does_not_claim_infinite_precision(self) -> None:
        assert newey_west_interval(
            [Decimal("0.1")] * 12,
            horizon_days=126,
        ) is None

    def test_hac_standard_error_exceeds_the_naive_one_for_persistent_series(self) -> None:
        """The whole point of the correction.

        If the HAC error were not larger, the overlapping-window bias would be
        unaddressed and every published interval would be too narrow.
        """

        hac = newey_west_standard_error(PERSISTENT, lag=5)
        naive = newey_west_standard_error(PERSISTENT, lag=0)
        assert hac is not None
        assert naive is not None
        assert hac > naive

    def test_interval_brackets_the_mean(self) -> None:
        interval = newey_west_interval(PERSISTENT, horizon_days=21)
        assert interval is not None
        assert interval.low < interval.point < interval.high
        assert interval.method == "newey_west"
        assert interval.sample_size == len(PERSISTENT)
        assert interval.effective_sample_size < Decimal(len(PERSISTENT))

    def test_wider_confidence_gives_a_wider_interval(self) -> None:
        narrow = newey_west_interval(PERSISTENT, horizon_days=21, confidence=Decimal("0.90"))
        wide = newey_west_interval(PERSISTENT, horizon_days=21, confidence=Decimal("0.99"))
        assert narrow is not None
        assert wide is not None
        assert (wide.high - wide.low) > (narrow.high - narrow.low)

    def test_too_short_a_series_yields_no_interval(self) -> None:
        assert newey_west_interval([Decimal("0.1")], horizon_days=21) is None


class TestBlockBootstrap:
    def test_replicates_are_reproducible_for_a_fixed_seed(self) -> None:
        seed = seed_from_text("auspex-performance")
        first = moving_block_bootstrap_means(PERSISTENT, block_size=5, replicates=50, seed=seed)
        second = moving_block_bootstrap_means(PERSISTENT, block_size=5, replicates=50, seed=seed)
        assert first == second
        assert len(first) == 50

    def test_a_different_seed_changes_the_draw(self) -> None:
        first = moving_block_bootstrap_means(PERSISTENT, block_size=5, replicates=50, seed=seed_from_text("a"))
        second = moving_block_bootstrap_means(PERSISTENT, block_size=5, replicates=50, seed=seed_from_text("b"))
        assert first != second

    def test_interval_is_deterministic_and_records_its_provenance(self) -> None:
        seed = seed_from_text("auspex-performance")
        interval = block_bootstrap_interval(PERSISTENT, horizon_days=5, seed=seed, replicates=200)
        again = block_bootstrap_interval(PERSISTENT, horizon_days=5, seed=seed, replicates=200)
        assert interval is not None
        assert again is not None
        assert (interval.low, interval.point, interval.high) == (again.low, again.point, again.high)
        assert interval.method == "moving_block_bootstrap"
        assert interval.seed == seed
        assert interval.replicates == 200

    def test_interval_brackets_the_point_estimate(self) -> None:
        interval = block_bootstrap_interval(
            PERSISTENT, horizon_days=5, seed=seed_from_text("bracket"), replicates=200
        )
        assert interval is not None
        assert interval.low <= interval.point <= interval.high

    def test_a_clearly_positive_series_excludes_zero(self) -> None:
        interval = block_bootstrap_interval(
            PERSISTENT, horizon_days=5, seed=seed_from_text("positive"), replicates=400
        )
        assert interval is not None
        assert interval.excludes_zero

    def test_too_short_a_series_yields_no_interval(self) -> None:
        assert block_bootstrap_interval([Decimal("0.1")], horizon_days=21, seed=1) is None


class TestInformationRatio:
    def test_is_mean_over_volatility(self) -> None:
        ratio = information_ratio([Decimal("0.02"), Decimal("0.04"), Decimal("0.06")])
        assert ratio is not None
        assert abs(ratio - Decimal("2")) < Decimal("0.0001")

    def test_is_undefined_for_a_constant_series(self) -> None:
        assert information_ratio([Decimal("0.02"), Decimal("0.02")]) is None


class TestICDistribution:
    def test_summarises_the_series_and_reports_overlap_adjusted_significance(self) -> None:
        result = ic_distribution(list(PERSISTENT), horizon_days=21)
        assert result is not None
        assert result.horizon_days == 21
        assert result.count == len(PERSISTENT)
        assert result.minimum == Decimal("-0.01")
        assert result.maximum == Decimal("0.07")
        assert result.q10 <= result.q25 <= result.median <= result.q75 <= result.q90
        assert result.positive_fraction > Decimal("0.8")
        assert result.icir is not None
        assert result.effective_sample_size < Decimal(result.count)
        assert result.t_statistic is not None
        assert result.p_value is None

    def test_ignores_dates_without_an_ic(self) -> None:
        result = ic_distribution([Decimal("0.02"), None, Decimal("0.04")], horizon_days=21)
        assert result is not None
        assert result.count == 2

    def test_a_constant_series_has_no_icir(self) -> None:
        result = ic_distribution([Decimal("0.05")] * 6, horizon_days=21)
        assert result is not None
        assert result.icir is None

    def test_no_usable_observations_yields_nothing(self) -> None:
        assert ic_distribution([None, None], horizon_days=21) is None

    def test_overlap_penalty_is_monotone_in_the_horizon(self) -> None:
        short = ic_distribution(list(PERSISTENT), horizon_days=21)
        long = ic_distribution(list(PERSISTENT), horizon_days=126)
        assert short is not None
        assert long is not None
        assert long.effective_sample_size <= short.effective_sample_size
        assert abs(long.t_statistic or Decimal(0)) <= abs(short.t_statistic or Decimal(0))
