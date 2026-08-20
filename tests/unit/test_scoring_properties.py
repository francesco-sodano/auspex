"""Property/invariant tests for the deterministic scoring core.

These are not example-based regressions: each test asserts a *property* that must
hold across a deterministically generated family of inputs. A fixed-seed
``random.Random`` stands in for a property-testing library so the suite stays
dependency-free and byte-for-byte reproducible — a failure here always
reproduces exactly.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from auspex.models.enums import Direction, FilerProfile, LegName
from auspex.scoring.composite import (
    REASON_NOT_APPLICABLE,
    REASON_RAW_MISSING,
    compute_security_composite,
)
from auspex.scoring.coverage import applicable_legs, coverage
from auspex.scoring.legs import FundamentalHealthInputs, fundamental_health
from auspex.scoring.normalize import (
    percentile_rank_fraction,
    shrinkage_lambda,
    shrinkage_tier_weights,
)
from auspex.scoring.sessions import contiguous_weakening_streak, prior_sessions

SEED = 20250611
CASES = 40


def _rng(offset: int = 0) -> random.Random:
    return random.Random(SEED + offset)


def _decimals(rng: random.Random, count: int, *, lo: int = -500, hi: int = 500) -> list[Decimal]:
    return [Decimal(rng.randint(lo, hi)) / Decimal(100) for _ in range(count)]


# Decimal division carries 28 significant digits, so algebraically identical
# expressions can differ in the final digit. Compare exact reasoning to that
# precision rather than to bit equality.
EPSILON = Decimal("1e-20")


def _close(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) < EPSILON


class TestPercentileRankProperties:
    """Req 4 — midpoint/tie-aware percentiles."""

    def test_fractions_are_strictly_inside_the_unit_interval(self) -> None:
        rng = _rng(1)
        for _ in range(CASES):
            population = _decimals(rng, rng.randint(1, 25))
            for value in population:
                fraction = percentile_rank_fraction(value, population)
                assert fraction is not None
                assert Decimal(0) < fraction < Decimal(1)

    def test_a_populations_own_fractions_average_to_one_half(self) -> None:
        # The midpoint convention is unbiased: no cohort size can shift the
        # centre of mass of its own ranks away from 50%.
        rng = _rng(2)
        tolerance = Decimal("1e-20")
        for _ in range(CASES):
            population = _decimals(rng, rng.randint(1, 20))
            fractions = [percentile_rank_fraction(v, population) for v in population]
            total = sum((f for f in fractions if f is not None), Decimal(0))
            mean = total / Decimal(len(population))
            assert abs(mean - Decimal("0.5")) < tolerance

    def test_rank_is_monotone_non_decreasing_in_value(self) -> None:
        rng = _rng(3)
        for _ in range(CASES):
            population = _decimals(rng, rng.randint(2, 20))
            probes = sorted(_decimals(rng, 5))
            ranks = [percentile_rank_fraction(p, population) for p in probes]
            assert all(r is not None for r in ranks)
            assert ranks == sorted(ranks)  # type: ignore[type-var]

    def test_tied_members_share_one_rank(self) -> None:
        rng = _rng(4)
        for _ in range(CASES):
            base = _decimals(rng, rng.randint(1, 8))
            tied = Decimal("7.5")
            population = [*base, tied, tied, tied]
            assert percentile_rank_fraction(tied, population) == percentile_rank_fraction(tied, population)
            below = sum(1 for v in population if v < tied)
            expected = (Decimal(below) + Decimal("1.5")) / Decimal(len(population))
            assert percentile_rank_fraction(tied, population) == expected


class TestShrinkageProperties:
    """Req 5 — continuous, explainable cohort-scope shrinkage."""

    def test_lambda_is_bounded_and_monotone_in_cohort_size(self) -> None:
        previous = shrinkage_lambda(0)
        assert previous == Decimal(0)
        for n in range(1, 200):
            current = shrinkage_lambda(n)
            assert Decimal(0) <= current < Decimal(1)
            assert current > previous
            previous = current

    def test_a_single_member_change_never_moves_lambda_by_a_cliff(self) -> None:
        # The whole point of req 5: no discrete fallback threshold.
        for n in range(1, 200):
            step = shrinkage_lambda(n + 1) - shrinkage_lambda(n)
            assert Decimal(0) < step < Decimal("0.1")

    def test_tier_weights_are_a_partition_of_unity(self) -> None:
        rng = _rng(5)
        for _ in range(CASES):
            lambda_cohort = Decimal(rng.randint(0, 100)) / Decimal(100)
            lambda_parent = Decimal(rng.randint(0, 100)) / Decimal(100)
            weights = shrinkage_tier_weights(lambda_cohort, lambda_parent)
            assert all(w >= 0 for w in weights)
            assert sum(weights, Decimal(0)) == Decimal(1)


class TestFundamentalHealthProperties:
    """Req 1 — standardise before equal-weight combination."""

    @staticmethod
    def _inputs(rng: random.Random) -> FundamentalHealthInputs:
        values = _decimals(rng, 5)
        return FundamentalHealthInputs(*values)  # type: ignore[arg-type]

    @staticmethod
    def _rescaled(
        source: FundamentalHealthInputs, field: str, scale: Decimal, shift: Decimal
    ) -> FundamentalHealthInputs:
        raw = getattr(source, field)
        return FundamentalHealthInputs(**{**source.as_map(), field: None if raw is None else raw * scale + shift})

    def test_is_invariant_to_affine_rescaling_of_any_sub_metric(self) -> None:
        # Rescaling one sub-metric across the *whole* cohort (a unit change) must
        # not move the leg. Raw-unit averaging would fail this outright.
        rng = _rng(6)
        for _ in range(CASES):
            cohort = {f"s{i}": self._inputs(rng) for i in range(8)}
            own = cohort["s0"]
            baseline = fundamental_health(own, cohort_inputs=cohort)

            scale = Decimal(rng.randint(2, 50))
            shift = Decimal(rng.randint(-40, 40))
            field = "fcf_margin"

            rescaled = {sid: self._rescaled(v, field, scale, shift) for sid, v in cohort.items()}
            after = fundamental_health(rescaled["s0"], cohort_inputs=rescaled)

            assert after.value == baseline.value
            assert after.available_sub_metrics == baseline.available_sub_metrics

    def test_missing_sub_metrics_are_traced_by_name_and_never_scored_as_zero(self) -> None:
        rng = _rng(7)
        for _ in range(CASES):
            cohort = {f"s{i}": self._inputs(rng) for i in range(8)}
            own = FundamentalHealthInputs(
                revenue_growth_yoy=Decimal("0.1"),
                gross_margin_trend_slope=None,
                fcf_margin=Decimal("0.2"),
                net_cash_ratio=None,
                roic=Decimal("0.3"),
            )
            cohort["s0"] = own
            result = fundamental_health(own, cohort_inputs=cohort)

            assert set(result.sub_metric_z) == {
                "revenue_growth_yoy",
                "gross_margin_trend_slope",
                "fcf_margin",
                "net_cash_ratio",
                "roic",
            }
            assert result.sub_metric_z["gross_margin_trend_slope"] is None
            assert result.sub_metric_z["net_cash_ratio"] is None
            assert result.available_sub_metrics <= 3
            assert result.sub_metric_coverage == Decimal(3) / Decimal(5)

    def test_below_the_minimum_the_leg_is_none_not_zero(self) -> None:
        rng = _rng(8)
        cohort = {f"s{i}": self._inputs(rng) for i in range(8)}
        own = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.1"),
            gross_margin_trend_slope=None,
            fcf_margin=None,
            net_cash_ratio=None,
            roic=None,
        )
        cohort["s0"] = own
        result = fundamental_health(own, cohort_inputs=cohort)
        assert result.value is None
        assert result.reason_not_computable is not None


class TestCompositeProperties:
    """Req 3 — neutral z = 0 for a missing leg, coverage kept separate."""

    WEIGHTS = {
        LegName.THESIS_LINKAGE: Decimal("0.30"),
        LegName.FUNDAMENTAL_HEALTH: Decimal("0.30"),
        LegName.SMART_MONEY: Decimal("0.20"),
        LegName.ATTENTION_ACCELERATION: Decimal("0.20"),
    }

    def _cohort(self, rng: random.Random) -> dict[LegName, dict[str, Decimal | None]]:
        return {
            leg: {f"s{i}": Decimal(rng.randint(-200, 200)) / Decimal(100) for i in range(6)} for leg in self.WEIGHTS
        }

    def test_applicable_denominator_is_independent_of_missing_data(self) -> None:
        rng = _rng(9)
        for _ in range(CASES):
            cohort = self._cohort(rng)
            full = {leg: cohort[leg]["s0"] for leg in self.WEIGHTS}
            dropped = dict(full)
            dropped[LegName.SMART_MONEY] = None

            complete = compute_security_composite(full, cohort, self.WEIGHTS, "s0")
            partial = compute_security_composite(dropped, cohort, self.WEIGHTS, "s0")

            # No renormalisation: the denominator does not shrink when a leg is
            # merely missing, so a security cannot upgrade itself by losing data.
            assert partial.weight_sum == complete.weight_sum
            assert partial.computable_weight < complete.computable_weight
            assert partial.legs[LegName.SMART_MONEY].contribution == Decimal(0)
            assert partial.legs[LegName.SMART_MONEY].reason_not_computable == REASON_RAW_MISSING

    def test_a_missing_leg_contributes_exactly_neutral_z_zero(self) -> None:
        rng = _rng(10)
        for _ in range(CASES):
            cohort = self._cohort(rng)
            full = {leg: cohort[leg]["s0"] for leg in self.WEIGHTS}
            complete = compute_security_composite(full, cohort, self.WEIGHTS, "s0")
            if complete.composite is None:
                continue

            dropped = dict(full)
            dropped[LegName.ATTENTION_ACCELERATION] = None
            partial = compute_security_composite(dropped, cohort, self.WEIGHTS, "s0")
            assert partial.composite is not None

            attention = complete.legs[LegName.ATTENTION_ACCELERATION]
            assert attention.contribution is not None

            # Dropping a leg removes exactly its contribution over the *unchanged*
            # applicable denominator. That is a neutral z = 0 substitution and
            # nothing else: no renormalisation, no reweighting of the survivors.
            expected = complete.composite - attention.contribution / complete.weight_sum
            assert _close(partial.composite, expected)

            surviving = sum(
                (
                    leg.contribution
                    for name, leg in partial.legs.items()
                    if name is not LegName.ATTENTION_ACCELERATION and leg.contribution is not None
                ),
                Decimal(0),
            )
            assert _close(partial.composite, surviving / partial.weight_sum)

    def test_a_leg_sitting_at_the_cohort_mean_is_indistinguishable_from_a_missing_one(
        self,
    ) -> None:
        # The sharpest statement of "neutral z = 0": a leg whose raw value is
        # exactly the cohort mean scores identically to one that is absent.
        rng = _rng(15)
        for _ in range(CASES):
            cohort = self._cohort(rng)
            # A value equal to the mean of the *other* members is a fixed point:
            # inserting it leaves the cohort mean unchanged, so its own z is 0.
            others = [v for sid, v in cohort[LegName.SMART_MONEY].items() if sid != "s0" and v is not None]
            mean = sum(others, Decimal(0)) / Decimal(len(others))

            at_mean = {leg: cohort[leg]["s0"] for leg in self.WEIGHTS}
            at_mean[LegName.SMART_MONEY] = mean
            cohort[LegName.SMART_MONEY]["s0"] = mean

            absent = dict(at_mean)
            absent[LegName.SMART_MONEY] = None

            neutral = compute_security_composite(at_mean, cohort, self.WEIGHTS, "s0")
            missing = compute_security_composite(absent, cohort, self.WEIGHTS, "s0")
            if neutral.composite is None or missing.composite is None:
                continue
            assert _close(neutral.composite, missing.composite)
            assert neutral.weight_sum == missing.weight_sum
            # …but coverage/confidence still tells them apart.
            assert neutral.computable_weight > missing.computable_weight

    def test_a_not_applicable_leg_leaves_the_denominator_entirely(self) -> None:
        rng = _rng(11)
        for _ in range(CASES):
            cohort = self._cohort(rng)
            full = {leg: cohort[leg]["s0"] for leg in self.WEIGHTS}
            excluded = compute_security_composite(
                full,
                cohort,
                self.WEIGHTS,
                "s0",
                not_applicable_legs=frozenset({LegName.THESIS_LINKAGE}),
            )
            assert excluded.weight_sum == Decimal(1) - self.WEIGHTS[LegName.THESIS_LINKAGE]
            leg = excluded.legs[LegName.THESIS_LINKAGE]
            assert leg.applicable is False
            assert leg.weight == Decimal(0)
            assert leg.reason_not_computable == REASON_NOT_APPLICABLE


class TestCoverageProperties:
    """Req 7 — structural exclusion must never be scored as a data failure."""

    def test_a_structurally_excluded_leg_cannot_lower_coverage(self) -> None:
        for profile in FilerProfile:
            every_leg = frozenset(applicable_legs(profile))
            for excluded_leg in every_leg:
                computable = set(every_leg - {excluded_leg})
                penalised = coverage(computable, filer_profile=profile)
                fair = coverage(
                    computable,
                    filer_profile=profile,
                    structural_exclusions=frozenset({excluded_leg}),
                )
                assert fair == Decimal(1)
                assert penalised < fair

    def test_fpi_applicability_is_unchanged_by_structural_exclusion(self) -> None:
        # Req 3 asked for FPI semantics to be preserved: an FPI never has a
        # SMART_MONEY leg, with or without structural exclusions.
        assert LegName.SMART_MONEY not in applicable_legs(FilerProfile.FPI)
        assert LegName.SMART_MONEY not in applicable_legs(FilerProfile.FPI, frozenset({LegName.VALUATION_BRAKE}))
        assert LegName.SMART_MONEY in applicable_legs(FilerProfile.DOMESTIC)

    def test_genuinely_missing_legs_still_reduce_coverage_when_excluding(self) -> None:
        legs = applicable_legs(FilerProfile.DOMESTIC)
        excluded = frozenset({LegName.VALUATION_BRAKE})
        remaining = [leg for leg in legs if leg not in excluded]
        computable = set(remaining[:-1])
        got = coverage(computable, FilerProfile.DOMESTIC, excluded)
        assert got == Decimal(len(remaining) - 1) / Decimal(len(remaining))
        assert got < Decimal(1)


class TestSessionProperties:
    """Req 6 — trading-session comparisons and true contiguity."""

    @staticmethod
    def _calendar(rng: random.Random, length: int) -> list[date]:
        start = date(2025, 1, 6)
        sessions: list[date] = []
        cursor = start
        while len(sessions) < length:
            if cursor.weekday() < 5 and rng.random() > 0.1:  # skip weekends + holidays
                sessions.append(cursor)
            cursor += timedelta(days=1)
        return sessions

    def test_prior_sessions_are_strictly_before_and_descending(self) -> None:
        rng = _rng(12)
        for _ in range(CASES):
            calendar = self._calendar(rng, 30)
            as_of = calendar[-1]
            got = prior_sessions(calendar, as_of, rng.randint(1, 10))
            assert all(d < as_of for d in got)
            assert list(got) == sorted(got, reverse=True)
            assert len(set(got)) == len(got)

    def test_a_gap_in_scores_can_never_lengthen_a_streak(self) -> None:
        rng = _rng(13)
        for _ in range(CASES):
            calendar = self._calendar(rng, 20)
            as_of = calendar[-1]
            directions = {d: Direction.WEAKENING for d in calendar}
            full = contiguous_weakening_streak(Direction.WEAKENING, directions, calendar, as_of)
            assert full == len(calendar)

            hole = calendar[-4]
            punched = {d: v for d, v in directions.items() if d != hole}
            broken = contiguous_weakening_streak(Direction.WEAKENING, punched, calendar, as_of)
            assert broken == 3
            assert broken < full

    def test_a_non_weakening_current_direction_is_always_zero(self) -> None:
        rng = _rng(14)
        calendar = self._calendar(rng, 10)
        directions = {d: Direction.WEAKENING for d in calendar}
        for direction in Direction:
            if direction is Direction.WEAKENING:
                continue
            assert contiguous_weakening_streak(direction, directions, calendar, calendar[-1]) == 0
