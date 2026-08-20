"""Unit tests for composite score computation and coverage (arc42 §5.5 "Composite", "Coverage")."""

from __future__ import annotations

from decimal import Decimal

from auspex.models.enums import Direction, FilerProfile, LegName
from auspex.scoring.composite import (
    REASON_DEGENERATE_CROSS_SECTION,
    REASON_NOT_APPLICABLE,
    REASON_RAW_MISSING,
    classify_direction,
    compute_percentile,
    compute_security_composite,
)
from auspex.scoring.coverage import applicable_legs, coverage, is_stale


class TestComputeSecurityComposite:
    def test_composite_weighted_average_of_z_scores(self):
        weights = {LegName.THESIS_LINKAGE: Decimal("0.5"), LegName.SMART_MONEY: Decimal("0.5")}
        leg_raw = {LegName.THESIS_LINKAGE: Decimal("0.8"), LegName.SMART_MONEY: Decimal("0.02")}
        cohort_raw = {
            LegName.THESIS_LINKAGE: {"a": Decimal("0.8"), "b": Decimal("0.4"), "c": Decimal("0.2")},
            LegName.SMART_MONEY: {"a": Decimal("0.02"), "b": Decimal("0.0"), "c": Decimal("-0.01")},
        }
        result = compute_security_composite(leg_raw, cohort_raw, weights, "a")
        assert result.composite is not None
        assert result.legs[LegName.THESIS_LINKAGE].computable
        assert result.legs[LegName.SMART_MONEY].computable

    def test_leg_non_computable_when_raw_is_none(self):
        weights = {LegName.THESIS_LINKAGE: Decimal("1.0")}
        leg_raw = {LegName.THESIS_LINKAGE: None}
        cohort_raw = {LegName.THESIS_LINKAGE: {"a": None, "b": Decimal("0.4")}}
        result = compute_security_composite(leg_raw, cohort_raw, weights, "a")
        assert not result.legs[LegName.THESIS_LINKAGE].computable
        assert result.composite is None  # no computable legs at all

    def test_composite_uses_neutral_zero_for_missing_leg(self):
        """A missing leg contributes neutral z=0; it does not reweight the rest.

        Renormalising over the available weights silently promoted whichever
        legs happened to be present. A security missing its smart-money leg had
        its thesis leg quietly doubled in importance, so an incomplete security
        could out-score a fully-covered one on the strength of a single good
        number. The applicable-weight denominator now stays fixed and the
        missing leg simply contributes nothing — coverage, reported separately,
        is what tells the reader the picture is incomplete.
        """

        weights = {LegName.THESIS_LINKAGE: Decimal("0.5"), LegName.SMART_MONEY: Decimal("0.5")}
        leg_raw = {LegName.THESIS_LINKAGE: Decimal("0.8"), LegName.SMART_MONEY: None}
        cohort_raw = {
            LegName.THESIS_LINKAGE: {"a": Decimal("0.8"), "b": Decimal("0.4"), "c": Decimal("0.2")},
            LegName.SMART_MONEY: {"a": None, "b": Decimal("0.0"), "c": None},
        }
        result = compute_security_composite(leg_raw, cohort_raw, weights, "a")

        # The denominator is every *applicable* leg, not just the computable ones.
        assert result.weight_sum == Decimal("1.0")
        assert result.computable_weight == Decimal("0.5")
        assert result.composite is not None

        missing = result.legs[LegName.SMART_MONEY]
        assert not missing.computable
        assert missing.applicable
        assert missing.contribution == Decimal(0)
        assert missing.z is None
        assert missing.reason_not_computable == REASON_RAW_MISSING

    def test_missing_leg_pulls_the_composite_toward_neutral(self):
        weights = {LegName.THESIS_LINKAGE: Decimal("0.5"), LegName.SMART_MONEY: Decimal("0.5")}
        cohort_raw = {
            LegName.THESIS_LINKAGE: {"a": Decimal("0.8"), "b": Decimal("0.4"), "c": Decimal("0.2")},
            LegName.SMART_MONEY: {"a": Decimal("0.02"), "b": Decimal("0.0"), "c": Decimal("-0.01")},
        }
        both = compute_security_composite(
            {LegName.THESIS_LINKAGE: Decimal("0.8"), LegName.SMART_MONEY: Decimal("0.02")},
            cohort_raw,
            weights,
            "a",
        )
        one = compute_security_composite(
            {LegName.THESIS_LINKAGE: Decimal("0.8"), LegName.SMART_MONEY: None},
            cohort_raw,
            weights,
            "a",
        )
        assert both.composite is not None and one.composite is not None
        # Both legs were positive for "a"; dropping one must not leave the score
        # unchanged (renormalisation) — it must move toward zero.
        assert abs(one.composite) < abs(both.composite)

    def test_structurally_excluded_leg_leaves_the_denominator(self):
        """A leg that does not apply is not a gap — it is simply absent."""

        weights = {LegName.THESIS_LINKAGE: Decimal("0.5"), LegName.VALUATION_BRAKE: Decimal("0.5")}
        leg_raw = {LegName.THESIS_LINKAGE: Decimal("0.8"), LegName.VALUATION_BRAKE: None}
        cohort_raw = {
            LegName.THESIS_LINKAGE: {"a": Decimal("0.8"), "b": Decimal("0.4"), "c": Decimal("0.2")},
            LegName.VALUATION_BRAKE: {"a": None, "b": Decimal("0.1"), "c": Decimal("0.3")},
        }
        result = compute_security_composite(
            leg_raw,
            cohort_raw,
            weights,
            "a",
            not_applicable_legs=frozenset({LegName.VALUATION_BRAKE}),
        )
        excluded = result.legs[LegName.VALUATION_BRAKE]
        assert not excluded.applicable
        assert excluded.weight == Decimal(0)
        assert excluded.contribution is None
        assert excluded.reason_not_computable == REASON_NOT_APPLICABLE
        assert result.weight_sum == Decimal("0.5")

    def test_degenerate_cross_section_is_reported_distinctly(self):
        weights = {LegName.THESIS_LINKAGE: Decimal("1.0")}
        leg_raw = {LegName.THESIS_LINKAGE: Decimal("0.5")}
        cohort_raw = {LegName.THESIS_LINKAGE: {"a": Decimal("0.5"), "b": Decimal("0.5")}}
        result = compute_security_composite(leg_raw, cohort_raw, weights, "a")
        leg = result.legs[LegName.THESIS_LINKAGE]
        assert not leg.computable
        assert leg.raw == Decimal("0.5")
        assert leg.reason_not_computable == REASON_DEGENERATE_CROSS_SECTION

    def test_winsorisation_caps_extreme_z(self):
        weights = {LegName.THESIS_LINKAGE: Decimal("1.0")}
        leg_raw = {LegName.THESIS_LINKAGE: Decimal("1000")}
        cohort_raw = {
            LegName.THESIS_LINKAGE: {
                "a": Decimal("1000"),
                **{f"peer{i}": Decimal("1") for i in range(9)},
            }
        }
        result = compute_security_composite(leg_raw, cohort_raw, weights, "a", winsor_sigma=Decimal("2.5"))
        assert result.legs[LegName.THESIS_LINKAGE].z == Decimal("2.5")


class TestDirection:
    def test_strengthening_when_delta_above_threshold(self):
        assert classify_direction(Decimal("0.20")) == Direction.STRENGTHENING

    def test_weakening_when_delta_below_threshold(self):
        assert classify_direction(Decimal("-0.20")) == Direction.WEAKENING

    def test_stable_within_band(self):
        assert classify_direction(Decimal("0.05")) == Direction.STABLE
        assert classify_direction(Decimal("-0.05")) == Direction.STABLE

    def test_stable_when_no_prior_value(self):
        assert classify_direction(None) == Direction.STABLE

    def test_boundary_exactly_at_threshold_is_stable(self):
        assert classify_direction(Decimal("0.15")) == Direction.STABLE
        assert classify_direction(Decimal("-0.15")) == Direction.STABLE


class TestPercentile:
    def test_percentile_none_when_own_composite_missing(self):
        assert compute_percentile("a", {"a": None, "b": Decimal(1)}) is None

    def test_percentile_computed_within_population(self):
        composites = {"a": Decimal(10), "b": Decimal(5), "c": Decimal(1)}
        # Midpoint convention: (2 + 0.5) / 3 = 83%, not a flat 100.
        assert compute_percentile("a", composites) == 83

    def test_best_of_a_small_population_is_not_reported_as_top_of_market(self):
        composites = {"a": Decimal(10), "b": Decimal(5), "c": Decimal(1)}
        assert compute_percentile("a", composites) < 100
        assert compute_percentile("c", composites) > 0


class TestCoverage:
    def test_domestic_has_six_applicable_legs(self):
        assert len(applicable_legs(FilerProfile.DOMESTIC)) == 6

    def test_fpi_has_five_applicable_legs_no_smart_money(self):
        legs = applicable_legs(FilerProfile.FPI)
        assert len(legs) == 5
        assert LegName.SMART_MONEY not in legs

    def test_full_coverage_domestic(self):
        computable = set(applicable_legs(FilerProfile.DOMESTIC))
        assert coverage(computable, FilerProfile.DOMESTIC) == Decimal(1)

    def test_partial_coverage_domestic(self):
        computable = {LegName.THESIS_LINKAGE, LegName.SMART_MONEY, LegName.FUNDAMENTAL_HEALTH}
        result = coverage(computable, FilerProfile.DOMESTIC)
        assert result == Decimal(3) / Decimal(6)

    def test_fpi_coverage_ignores_smart_money(self):
        computable = {
            LegName.THESIS_LINKAGE,
            LegName.ATTENTION_ACCELERATION,
            LegName.NARRATIVE_PREMIUM,
            LegName.FUNDAMENTAL_HEALTH,
            LegName.VALUATION_BRAKE,
        }
        assert coverage(computable, FilerProfile.FPI) == Decimal(1)

    def test_buy_eligibility_threshold_is_080(self):
        from auspex.scoring.coverage import MIN_COVERAGE_FOR_BUY

        assert MIN_COVERAGE_FOR_BUY == Decimal("0.80")


class TestStaleness:
    def test_within_two_sessions_not_stale(self):
        assert is_stale(None, None, trading_sessions_between=2) is False

    def test_more_than_two_sessions_is_stale(self):
        assert is_stale(None, None, trading_sessions_between=3) is True
