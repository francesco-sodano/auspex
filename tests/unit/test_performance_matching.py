"""Matched-population IC and correlation (arc42 §5.8).

These tests pin the two statistical defects this workstream exists to fix:

* per-leg IC used to require *every* candidate to carry the leg, so a leg that
  is structurally absent for some filers (SMART_MONEY on FPIs) silently
  discarded whole dates;
* leg correlation used to pool an all-legs-complete population across dates,
  which both shrank the sample and let time-series drift show up as
  cross-sectional redundancy.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from auspex.models.enums import LegName
from auspex.performance.matching import (
    aggregate_pair_correlations,
    matched_composite_ic,
    matched_leg_ic,
    matched_pair_correlation,
    matched_pairs,
    per_date_pair_correlations,
)

AS_OF = date(2026, 3, 2)


class TestMatchedPairs:
    def test_intersects_and_sorts_by_security_id(self) -> None:
        left = {"c": Decimal("1"), "a": Decimal("2"), "b": Decimal("3")}
        right = {"b": Decimal("30"), "a": Decimal("20"), "z": Decimal("99")}
        xs, ys, ids = matched_pairs(left, right)
        assert ids == ["a", "b"]
        assert xs == [Decimal("2"), Decimal("3")]
        assert ys == [Decimal("20"), Decimal("30")]

    def test_disjoint_inputs_yield_nothing(self) -> None:
        assert matched_pairs({"a": Decimal("1")}, {"b": Decimal("1")}) == ([], [], [])


class TestMatchedLegIC:
    def test_uses_the_legs_own_population_not_every_candidate(self) -> None:
        """Defect #1 regression.

        Three names have a forward return, only two carry the leg. The old
        dense path produced nothing at all for this date; the matched path
        produces a real IC on the two names that actually have the leg, and
        reports the coverage so the reader can discount it.
        """

        leg_z = {"AAA": Decimal("1"), "BBB": Decimal("-1")}
        returns = {"AAA": Decimal("0.10"), "BBB": Decimal("-0.05"), "CCC": Decimal("0.02")}
        result = matched_leg_ic(AS_OF, leg_z, returns)
        assert result.value == Decimal("1")
        assert result.population == 2
        assert result.candidates == 3
        assert result.coverage_fraction is not None
        assert abs(result.coverage_fraction - Decimal("0.6666666")) < Decimal("0.0001")

    def test_reports_none_when_the_matched_population_is_too_small(self) -> None:
        result = matched_leg_ic(AS_OF, {"AAA": Decimal("1")}, {"AAA": Decimal("0.1"), "BBB": Decimal("0.2")})
        assert result.value is None
        assert result.population == 1
        assert result.candidates == 2

    def test_coverage_fraction_is_none_without_candidates(self) -> None:
        assert matched_leg_ic(AS_OF, {}, {}).coverage_fraction is None

    def test_perfect_inversion_gives_minus_one(self) -> None:
        leg_z = {"AAA": Decimal("3"), "BBB": Decimal("2"), "CCC": Decimal("1")}
        returns = {"AAA": Decimal("-0.03"), "BBB": Decimal("0.00"), "CCC": Decimal("0.05")}
        value = matched_leg_ic(AS_OF, leg_z, returns).value
        assert value is not None
        assert abs(value - Decimal("-1")) < Decimal("0.000001")


class TestMatchedCompositeIC:
    def test_matches_on_shared_names_only(self) -> None:
        percentiles = {"AAA": Decimal("90"), "BBB": Decimal("50"), "CCC": Decimal("10")}
        returns = {"AAA": Decimal("0.09"), "BBB": Decimal("0.01")}
        result = matched_composite_ic(AS_OF, percentiles, returns)
        assert result.population == 2
        assert result.candidates == 2
        assert result.value == Decimal("1")


class TestPairCorrelation:
    def test_measures_only_the_overlap(self) -> None:
        values_a = {"AAA": Decimal("1"), "BBB": Decimal("2"), "CCC": Decimal("3")}
        values_b = {"AAA": Decimal("2"), "BBB": Decimal("4")}
        result = matched_pair_correlation(LegName.THESIS_LINKAGE, LegName.SMART_MONEY, values_a, values_b)
        assert result.population == 2
        assert result.value == Decimal("1")

    def test_per_date_emits_each_distinct_pair_once(self) -> None:
        by_leg = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("2")},
            LegName.SMART_MONEY: {"AAA": Decimal("2"), "BBB": Decimal("1")},
            LegName.VALUATION_BRAKE: {"AAA": Decimal("1"), "BBB": Decimal("3")},
        }
        pairs = per_date_pair_correlations(by_leg)
        assert len(pairs) == 3
        for leg_a, leg_b in pairs:
            assert leg_a.value < leg_b.value


class TestAggregatePairCorrelations:
    def test_per_date_average_differs_from_the_pooled_estimate(self) -> None:
        """Defect #2 regression.

        Within each date the two legs are perfectly *negatively* correlated.
        Pooling both dates into one population hides that behind a level shift
        and reports a strong positive correlation instead. The per-date
        estimator must report -1.
        """

        day_one = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("2")},
            LegName.SMART_MONEY: {"AAA": Decimal("2"), "BBB": Decimal("1")},
        }
        day_two = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("11"), "BBB": Decimal("12")},
            LegName.SMART_MONEY: {"AAA": Decimal("12"), "BBB": Decimal("11")},
        }
        aggregated = aggregate_pair_correlations({date(2026, 3, 2): day_one, date(2026, 3, 3): day_two})
        pair = aggregated[(LegName.SMART_MONEY, LegName.THESIS_LINKAGE)]
        assert pair.mean_correlation == Decimal("-1")
        assert pair.dates_used == 2

        # The pooled population the old code used would have said +1.
        from auspex.performance.ic import pearson

        pooled_a = [Decimal("1"), Decimal("2"), Decimal("11"), Decimal("12")]
        pooled_b = [Decimal("2"), Decimal("1"), Decimal("12"), Decimal("11")]
        pooled = pearson(pooled_a, pooled_b)
        assert pooled is not None
        assert pooled > Decimal("0.9")

    def test_reports_population_bounds_per_pair(self) -> None:
        day_one = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("2")},
            LegName.SMART_MONEY: {"AAA": Decimal("1"), "BBB": Decimal("2")},
        }
        day_two = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("2"), "CCC": Decimal("3")},
            LegName.SMART_MONEY: {"AAA": Decimal("1"), "BBB": Decimal("2"), "CCC": Decimal("3")},
        }
        aggregated = aggregate_pair_correlations({date(2026, 3, 2): day_one, date(2026, 3, 3): day_two})
        pair = aggregated[(LegName.SMART_MONEY, LegName.THESIS_LINKAGE)]
        assert pair.min_population == 2
        assert pair.max_population == 3
        assert pair.total_observations == 5

    def test_dates_without_a_defined_correlation_are_skipped(self) -> None:
        flat = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("1")},
            LegName.SMART_MONEY: {"AAA": Decimal("1"), "BBB": Decimal("2")},
        }
        good = {
            LegName.THESIS_LINKAGE: {"AAA": Decimal("1"), "BBB": Decimal("2")},
            LegName.SMART_MONEY: {"AAA": Decimal("1"), "BBB": Decimal("2")},
        }
        aggregated = aggregate_pair_correlations({date(2026, 3, 2): flat, date(2026, 3, 3): good})
        pair = aggregated[(LegName.SMART_MONEY, LegName.THESIS_LINKAGE)]
        assert pair.dates_used == 1
        assert pair.mean_correlation == Decimal("1")

    def test_empty_input_is_empty_output(self) -> None:
        assert aggregate_pair_correlations({}) == {}
