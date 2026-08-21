"""Pre-registered champion/challenger shadow validation (arc42 §5.8).

Nothing here may influence production scores. These tests pin that guarantee
(weights are asserted against the published configuration, and the study is
fingerprinted before it is run) alongside the statistics that decide whether a
challenger is genuinely better or merely luckier.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from auspex.models.enums import LegName
from auspex.performance.shadow import (
    CHAMPION,
    CHAMPION_VARIANT,
    CORRECTED_FIXED,
    CORRECTED_FIXED_VARIANT,
    PRODUCTION_DOMESTIC_WEIGHTS,
    SHADOW_METRIC_TYPE,
    PreRegistration,
    ShadowCrossSection,
    ShadowVariant,
    assert_matches_production_weights,
    default_pre_registration,
    promotion_verdict,
    run_shadow_comparison,
    score_variant,
    shadow_metrics,
)

START = date(2026, 1, 5)
REGISTERED = date(2026, 1, 2)
NAMES = [f"S{index:02d}" for index in range(10)]
WEIGHTED_LEGS = tuple(PRODUCTION_DOMESTIC_WEIGHTS)


def _cross_sections(days: int = 20, *, missing_leg_for: set[str] | None = None) -> list[ShadowCrossSection]:
    """Dates where the composite ranks returns, with a leg missing for some names."""

    missing = missing_leg_for or set()
    sections: list[ShadowCrossSection] = []
    for day in range(days):
        leg_z: dict[str, dict[LegName, Decimal]] = {}
        champion: dict[str, Decimal] = {}
        returns: dict[str, Decimal] = {}
        for index, sid in enumerate(NAMES):
            wobble = Decimal((index * 3 + day * 7) % 5) / Decimal("100")
            base = Decimal(index) / Decimal("10") + wobble
            legs = {leg: base for leg in WEIGHTED_LEGS}
            if sid in missing:
                legs.pop(LegName.SMART_MONEY, None)
            leg_z[sid] = legs
            champion[sid] = base
            returns[sid] = Decimal(index) / Decimal("100") + wobble
        sections.append(
            ShadowCrossSection(
                as_of_date=START + timedelta(days=day),
                champion_scores_by_security=champion,
                leg_z_by_security=leg_z,
                forward_returns_usd_by_horizon={21: returns, 63: returns, 126: returns},
            )
        )
    return sections


class TestProductionWeightGuard:
    def test_accepts_the_published_weights(self) -> None:
        assert_matches_production_weights(dict(PRODUCTION_DOMESTIC_WEIGHTS))

    def test_rejects_any_drift(self) -> None:
        """A shadow study is only meaningful against the live champion.

        If production weights move and the frozen copy does not, the study is
        silently comparing challengers to a champion that no longer exists.
        """

        drifted = dict(PRODUCTION_DOMESTIC_WEIGHTS)
        drifted[LegName.THESIS_LINKAGE] = drifted[LegName.THESIS_LINKAGE] + Decimal("0.01")
        with pytest.raises(ValueError):
            assert_matches_production_weights(drifted)

    def test_rejects_a_missing_leg(self) -> None:
        partial = {leg: weight for leg, weight in PRODUCTION_DOMESTIC_WEIGHTS.items() if leg is not LegName.SMART_MONEY}
        with pytest.raises(ValueError):
            assert_matches_production_weights(partial)


class TestScoreVariant:
    def test_the_champion_variant_replays_the_stored_score_untouched(self) -> None:
        """The champion must never be recomputed.

        Re-deriving it would let a bug in this module masquerade as a
        difference between champion and challenger.
        """

        section = _cross_sections(days=1)[0]
        assert score_variant(CHAMPION_VARIANT, section) == section.champion_scores_by_security

    def test_corrected_fixed_only_differs_where_a_leg_is_missing(self) -> None:
        missing = {NAMES[3], NAMES[4]}
        section = _cross_sections(days=1, missing_leg_for=missing)[0]
        champion_like = ShadowVariant(
            name="legacy_computable_denominator",
            description="legacy production denominator",
            weights=dict(PRODUCTION_DOMESTIC_WEIGHTS),
            renormalise_on_computable=True,
        )
        baseline = score_variant(champion_like, section)
        corrected = score_variant(CORRECTED_FIXED_VARIANT, section)

        for sid in NAMES:
            if sid in missing:
                assert baseline[sid] != corrected[sid]
            else:
                assert baseline[sid] == corrected[sid]

    def test_a_security_with_no_computable_leg_is_dropped(self) -> None:
        section = ShadowCrossSection(
            as_of_date=START,
            champion_scores_by_security={"A": Decimal("1")},
            leg_z_by_security={"A": {}},
            forward_returns_usd_by_horizon={21: {"A": Decimal("0.01")}},
        )
        assert score_variant(CORRECTED_FIXED_VARIANT, section) == {}

    def test_inapplicable_legs_leave_the_denominator(self) -> None:
        legs = {leg: Decimal("1") for leg in WEIGHTED_LEGS}
        section = ShadowCrossSection(
            as_of_date=START,
            champion_scores_by_security={"A": Decimal("0")},
            leg_z_by_security={"A": legs},
            forward_returns_usd_by_horizon={21: {"A": Decimal("0.01")}},
            applicable_legs_by_security={"A": frozenset(WEIGHTED_LEGS) - {LegName.SMART_MONEY}},
        )
        scores = score_variant(CORRECTED_FIXED_VARIANT, section)
        assert abs(scores["A"] - Decimal("1")) < Decimal("0.000001")


class TestPreRegistration:
    def test_must_include_the_champion_baseline(self) -> None:
        with pytest.raises(ValueError):
            PreRegistration(
                study_id="x",
                hypothesis="h",
                primary_metric="m",
                decision_rule="d",
                variants=(CORRECTED_FIXED_VARIANT,),
                registered_on=REGISTERED,
            )

    def test_rejects_duplicate_variant_names(self) -> None:
        with pytest.raises(ValueError):
            PreRegistration(
                study_id="x",
                hypothesis="h",
                primary_metric="m",
                decision_rule="d",
                variants=(CHAMPION_VARIANT, CHAMPION_VARIANT),
                registered_on=REGISTERED,
            )

    def test_fingerprint_is_stable_and_sensitive(self) -> None:
        first = default_pre_registration(REGISTERED)
        second = default_pre_registration(REGISTERED)
        assert first.fingerprint == second.fingerprint
        assert first.fingerprint.startswith("sha256:")

        challenger = ShadowVariant(name="c1", description="challenger", weights=dict(PRODUCTION_DOMESTIC_WEIGHTS))
        with_challenger = default_pre_registration(REGISTERED, challengers=(challenger,))
        assert with_challenger.fingerprint != first.fingerprint

    def test_default_study_registers_champion_and_corrected_fixed(self) -> None:
        registration = default_pre_registration(REGISTERED)
        assert registration.variant_names[:2] == (CHAMPION, CORRECTED_FIXED)
        assert registration.horizons == (21, 63, 126)

    def test_named_challengers_are_carried_into_the_registration(self) -> None:
        challenger = ShadowVariant(name="equal_weight_legs", description="equal leg weights")
        registration = default_pre_registration(REGISTERED, challengers=(challenger,))
        assert "equal_weight_legs" in registration.variant_names
        payload = registration.registration_payload()
        assert any(item["name"] == "equal_weight_legs" for item in payload["variants"])  # type: ignore[index,union-attr]


class TestRunShadowComparison:
    def test_reports_one_result_per_variant_and_horizon(self) -> None:
        registration = default_pre_registration(REGISTERED)
        report = run_shadow_comparison(registration, _cross_sections())
        assert report.dates_evaluated == 20
        assert report.fingerprint == registration.fingerprint
        assert {(r.variant, r.horizon_days) for r in report.results} == {
            (name, horizon) for name in registration.variant_names for horizon in registration.horizons
        }

    def test_the_champion_is_not_compared_against_itself(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections())
        assert all(comparison.variant != CHAMPION for comparison in report.comparisons)

    def test_is_deterministic_for_a_fixed_registration(self) -> None:
        registration = default_pre_registration(REGISTERED)
        first = run_shadow_comparison(registration, _cross_sections())
        second = run_shadow_comparison(registration, _cross_sections())
        assert [(c.variant, c.horizon_days, c.mean_difference, c.bootstrap_low) for c in first.comparisons] == [
            (c.variant, c.horizon_days, c.mean_difference, c.bootstrap_low) for c in second.comparisons
        ]

    def test_too_few_dates_marks_the_study_underpowered(self) -> None:
        """Twenty-one-day returns on daily dates are heavily overlapping.

        A handful of dates cannot support a promotion decision, so the report
        must say so rather than emit a confident-looking mean.
        """

        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections(days=4))
        assert report.underpowered is True

    def test_an_adequate_run_is_not_underpowered(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections(days=20))
        assert report.underpowered is False

    def test_no_dates_produces_an_empty_but_valid_report(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), [])
        assert report.dates_evaluated == 0
        assert report.underpowered is True
        assert report.as_of_date is None


class TestPromotionVerdict:
    def test_an_underpowered_study_never_promotes(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections(days=4))
        for comparison in report.comparisons:
            assert promotion_verdict(report, comparison) in {
                "insufficient_evidence",
                "not_primary",
            }

    def test_an_indistinguishable_challenger_is_not_promoted(self) -> None:
        """``corrected_fixed`` cannot differ when no leg is missing.

        Every difference is exactly zero, so the honest verdict is "no
        improvement", not a coin-flip promotion.
        """

        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections(days=20))
        verdicts = {promotion_verdict(report, comparison) for comparison in report.comparisons}
        assert verdicts <= {
            "no_improvement",
            "promote",
            "insufficient_evidence",
            "not_primary",
        }
        assert "promote" not in verdicts

    def test_only_registered_primary_horizon_can_promote(self) -> None:
        report = run_shadow_comparison(
            default_pre_registration(REGISTERED),
            _cross_sections(days=20),
        )

        assert {
            promotion_verdict(report, comparison)
            for comparison in report.comparisons
            if comparison.horizon_days != 126
        } == {"not_primary"}


class TestShadowMetrics:
    def test_publishes_uniquely_identified_shadow_rows(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections())
        metrics = shadow_metrics(report)
        assert metrics
        assert len({metric.id for metric in metrics}) == len(metrics)
        assert {metric.metric_type for metric in metrics} == {SHADOW_METRIC_TYPE}

    def test_every_row_carries_the_registration_fingerprint(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), _cross_sections())
        for metric in shadow_metrics(report):
            assert metric.detail["fingerprint"] == report.fingerprint
            assert metric.detail["study_id"] == "shadow-v4.2-neutral-missing-v1"

    def test_an_empty_report_publishes_nothing(self) -> None:
        report = run_shadow_comparison(default_pre_registration(REGISTERED), [])
        assert shadow_metrics(report) == []
