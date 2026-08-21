"""Spread, benchmark, FDR, coverage-bias and detail-payload helpers (arc42 §5.8)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from auspex.performance.benchmarks import (
    equal_weight_return,
    momentum_ic,
    paired_comparison,
    random_null_band,
    random_ranking_ics,
)
from auspex.performance.coverage_bias import coverage_bias
from auspex.performance.detail import DETAILED_METRICS_VERSION, decimal_str, detail_payload
from auspex.performance.multiple_testing import benjamini_hochberg, holm_bonferroni
from auspex.performance.spread import (
    cost_adjusted_return,
    max_drawdown,
    top_minus_bottom,
    turnover,
)
from auspex.performance.stats import seed_from_text


def _ladder(count: int) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Scores and returns that agree perfectly, so the spread must be positive."""

    scores = {f"S{index:02d}": Decimal(index) for index in range(count)}
    returns = {f"S{index:02d}": Decimal(index) / Decimal("100") for index in range(count)}
    return scores, returns


class TestTopMinusBottom:
    def test_spread_is_positive_when_the_score_ranks_the_return(self) -> None:
        scores, returns = _ladder(10)
        result = top_minus_bottom(scores, returns)
        assert result is not None
        assert result.population == 10
        assert result.top_count == 2
        assert result.bottom_count == 2
        assert result.spread > Decimal("0")
        assert result.top_ids == ("S08", "S09")
        assert result.bottom_ids == ("S00", "S01")

    def test_robust_spread_discounts_a_single_blow_up(self) -> None:
        """One 500% print should not be allowed to define the strategy.

        The robust variant trims beyond ``outlier_sigma`` sample deviations and
        reports how many observations it dropped, so the reader sees the raw and
        trimmed numbers side by side rather than only a flattering one.
        """

        scores, returns = _ladder(30)
        returns["S29"] = Decimal("5.00")
        result = top_minus_bottom(scores, returns, outlier_sigma=Decimal("1.5"))
        assert result is not None
        assert result.top_count == 6
        assert result.spread > Decimal("0.7")
        assert result.robust_spread is not None
        assert result.robust_spread < result.spread
        assert result.outlier_count >= 1

    def test_needs_at_least_two_matched_names(self) -> None:
        assert top_minus_bottom({"A": Decimal("1")}, {"A": Decimal("0.1")}) is None

    def test_unmatched_names_are_ignored(self) -> None:
        scores, returns = _ladder(10)
        scores["GHOST"] = Decimal("99")
        result = top_minus_bottom(scores, returns)
        assert result is not None
        assert result.population == 10


class TestTurnover:
    def test_first_date_has_no_turnover(self) -> None:
        assert turnover(None, ("A", "B")) is None

    def test_identical_baskets_have_zero_turnover(self) -> None:
        assert turnover(("A", "B"), ("A", "B")) == Decimal("0")

    def test_full_replacement_is_one(self) -> None:
        assert turnover(("A", "B"), ("C", "D")) == Decimal("1")

    def test_half_replacement_is_one_half(self) -> None:
        assert turnover(("A", "B"), ("B", "C")) == Decimal("0.5")


class TestCostAdjustment:
    def test_costs_are_subtracted_in_proportion_to_turnover(self) -> None:
        assert cost_adjusted_return(Decimal("0.10"), Decimal("0.5"), Decimal("0.02")) == Decimal("0.09")

    def test_absent_inputs_yield_no_number_rather_than_a_guess(self) -> None:
        assert cost_adjusted_return(Decimal("0.10"), None, Decimal("0.02")) is None
        assert cost_adjusted_return(Decimal("0.10"), Decimal("0.5"), None) is None


class TestMaxDrawdown:
    def test_a_monotone_rise_never_draws_down(self) -> None:
        assert max_drawdown([Decimal("0.01"), Decimal("0.02")]) == Decimal("0")

    def test_measures_the_worst_peak_to_trough(self) -> None:
        drawdown = max_drawdown([Decimal("0.10"), Decimal("-0.20"), Decimal("0.05")])
        assert drawdown is not None
        assert drawdown > Decimal("0.15")

    def test_empty_history_has_no_drawdown(self) -> None:
        assert max_drawdown([]) is None

    def test_one_observation_has_no_path_drawdown(self) -> None:
        assert max_drawdown([Decimal("-0.20")]) is None


class TestBenchmarks:
    def test_equal_weight_return_is_the_simple_average(self) -> None:
        assert equal_weight_return({"A": Decimal("0.10"), "B": Decimal("0.20")}) == Decimal("0.15")

    def test_equal_weight_return_of_nothing_is_none(self) -> None:
        assert equal_weight_return({}) is None

    def test_momentum_ic_ranks_forward_returns_by_trailing_returns(self) -> None:
        trailing = {"A": Decimal("0.1"), "B": Decimal("0.2"), "C": Decimal("0.3")}
        forward = {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.03")}
        value = momentum_ic(trailing, forward)
        assert value is not None
        assert abs(value - Decimal("1")) < Decimal("0.000001")

    def test_random_rankings_are_reproducible(self) -> None:
        _scores, returns = _ladder(8)
        seed = seed_from_text("random-benchmark")
        first = random_ranking_ics(returns, seed=seed, replicates=25)
        second = random_ranking_ics(returns, seed=seed, replicates=25)
        assert first == second
        assert len(first) == 25

    def test_random_null_band_reports_a_positive_noise_threshold(self) -> None:
        _scores, returns = _ladder(8)
        band = random_null_band(
            {date(2026, 3, 2): returns, date(2026, 3, 3): returns},
            seed=seed_from_text("null-band"),
            replicates=50,
        )
        assert band is not None
        assert band.replicates > 0
        assert band.p95_absolute > Decimal("0")

    def test_paired_comparison_matches_on_dates(self) -> None:
        champion = {date(2026, 3, 2): Decimal("0.05"), date(2026, 3, 3): Decimal("0.03")}
        benchmark = {date(2026, 3, 2): Decimal("0.01"), date(2026, 3, 4): Decimal("0.09")}
        result = paired_comparison("equal_weight", champion, benchmark)
        assert result is not None
        assert result.count == 1
        assert result.mean_difference == Decimal("0.04")
        assert result.win_fraction == Decimal("1")

    def test_paired_comparison_without_shared_dates_is_none(self) -> None:
        assert paired_comparison("x", {date(2026, 3, 2): Decimal("1")}, {date(2026, 3, 3): Decimal("1")}) is None


class TestMultipleTesting:
    def test_benjamini_hochberg_is_less_conservative_than_holm(self) -> None:
        """Six horizons times six legs is a lot of chances to get lucky.

        Both procedures must control something; BH controls the false discovery
        rate and so should reject at least as much as Holm's FWER control.
        """

        p_values = {
            "a": Decimal("0.001"),
            "b": Decimal("0.008"),
            "c": Decimal("0.020"),
            "d": Decimal("0.400"),
            "e": Decimal("0.900"),
        }
        bh = {result.label: result.rejected for result in benjamini_hochberg(p_values)}
        holm = {result.label: result.rejected for result in holm_bonferroni(p_values)}
        assert sum(bh.values()) >= sum(holm.values())
        assert bh["a"] is True
        assert bh["e"] is False

    def test_q_values_are_monotone_in_rank(self) -> None:
        results = benjamini_hochberg({"a": Decimal("0.01"), "b": Decimal("0.02"), "c": Decimal("0.03")})
        assert [result.rank for result in results] == [1, 2, 3]
        assert results[0].q_value <= results[1].q_value <= results[2].q_value

    def test_no_tests_means_no_results(self) -> None:
        assert benjamini_hochberg({}) == []
        assert holm_bonferroni({}) == []


class TestCoverageBias:
    def test_detects_that_well_covered_names_score_better(self) -> None:
        """Coverage is not randomly assigned across the universe.

        If high-coverage names carry the entire IC, the headline number is a
        statement about data availability, not about the signal.
        """

        coverage = {f"S{i}": Decimal(i) / Decimal("10") for i in range(10)}
        scores = {f"S{i}": Decimal(i) for i in range(10)}
        returns = {f"S{i}": Decimal(i) / Decimal("100") for i in range(5)}
        returns.update({f"S{i}": Decimal(9 - i) / Decimal("100") for i in range(5, 10)})
        result = coverage_bias(coverage, scores, returns)
        assert result is not None
        assert result.population == 10
        assert result.high_coverage_population > 0
        assert result.low_coverage_population > 0
        assert result.high_coverage_ic is not None
        assert result.low_coverage_ic is not None
        assert result.ic_difference is not None

    def test_returns_nothing_without_a_matched_population(self) -> None:
        assert coverage_bias({}, {}, {}) is None


class TestDetailPayload:
    def test_version_is_published(self) -> None:
        assert DETAILED_METRICS_VERSION

    def test_decimal_str_drops_exponent_noise(self) -> None:
        assert decimal_str(Decimal("1.500")) == "1.5"
        assert decimal_str(Decimal("2E+1")) == "20"

    def test_payload_is_all_strings_and_drops_nones(self) -> None:
        payload = detail_payload(alpha=Decimal("0.05"), beta=None, gamma=3, delta=True, epsilon="x")
        assert payload == {"alpha": "0.05", "gamma": "3", "delta": "true", "epsilon": "x"}
        assert all(isinstance(value, str) for value in payload.values())
