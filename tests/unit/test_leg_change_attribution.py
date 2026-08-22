"""Unit tests for the leg-change attribution (arc42 §5.5 "Leg change").

``leg_changes`` rows carry ``own_evidence_effect`` and
``cohort_distribution_effect``. They used to be set to ``delta_z`` and ``"0"``
unconditionally, which told every reader that the issuer had moved even on days
when only its peer group had. These tests pin the replacement: an exact
two-term identity, or an explicit "unavailable" with no partial credit.
"""

from __future__ import annotations

import random
from decimal import Decimal

from auspex.scoring.composite import (
    ATTRIBUTION_QUANTUM,
    REASON_DECOMPOSITION_NO_CROSS_SECTION,
    REASON_DECOMPOSITION_NO_CURRENT,
    REASON_DECOMPOSITION_NO_PRIOR,
    LegCrossSection,
    decompose_leg_delta,
)

SEED = 20260822
CASES = 60


def _cross_section(values: list[Decimal], **kwargs) -> LegCrossSection:
    return LegCrossSection(cohort_values=tuple(values), **kwargs)


class TestExactness:
    def test_the_two_effects_sum_to_delta_z(self):
        prior_cross = _cross_section([Decimal(1), Decimal(2), Decimal(3), Decimal(4)])
        current_cross = _cross_section([Decimal(2), Decimal(5), Decimal(9), Decimal(14)])

        prior_raw = Decimal(2)
        current_raw = Decimal(9)
        prior_z = prior_cross.z_for(prior_raw)
        current_z = current_cross.z_for(current_raw)

        result = decompose_leg_delta(
            prior_z=prior_z,
            current_z=current_z,
            prior_raw=prior_raw,
            current_cross_section=current_cross,
        )

        assert result.own_evidence_effect is not None
        assert result.cohort_distribution_effect is not None
        assert result.own_evidence_effect + result.cohort_distribution_effect == result.delta_z
        # Published in fixed point so the three numbers reconcile as stored.
        assert result.delta_z == (current_z - prior_z).quantize(ATTRIBUTION_QUANTUM)
        assert result.reason_unavailable is None

    def test_only_the_issuer_moving_attributes_nothing_to_the_cohort(self):
        """Peers identical between the two days: the whole move is own evidence."""

        cross = _cross_section([Decimal(1), Decimal(2), Decimal(3), Decimal(4)])
        prior_raw = Decimal(1)
        current_raw = Decimal(4)

        result = decompose_leg_delta(
            prior_z=cross.z_for(prior_raw),
            current_z=cross.z_for(current_raw),
            prior_raw=prior_raw,
            current_cross_section=cross,
        )

        assert result.cohort_distribution_effect == Decimal(0)
        assert result.own_evidence_effect == result.delta_z

    def test_only_the_cohort_moving_attributes_nothing_to_the_issuer(self):
        """The headline case the old code got backwards.

        The issuer published nothing; its raw value is unchanged. Its z-score
        still moved because the peer distribution shifted underneath it, and
        the whole delta must be reported as such.
        """

        prior_cross = _cross_section([Decimal(1), Decimal(2), Decimal(3), Decimal(4)])
        current_cross = _cross_section([Decimal(10), Decimal(20), Decimal(30), Decimal(40)])
        unchanged_raw = Decimal(3)

        result = decompose_leg_delta(
            prior_z=prior_cross.z_for(unchanged_raw),
            current_z=current_cross.z_for(unchanged_raw),
            prior_raw=unchanged_raw,
            current_cross_section=current_cross,
        )

        assert result.own_evidence_effect == Decimal(0)
        assert result.cohort_distribution_effect == result.delta_z
        assert result.delta_z != Decimal(0)

    def test_membership_change_alone_is_a_distribution_effect(self):
        """Peers joining or leaving is the cohort moving, not the issuer."""

        prior_cross = _cross_section([Decimal(1), Decimal(2), Decimal(3)])
        current_cross = _cross_section([Decimal(1), Decimal(2), Decimal(3), Decimal(50), Decimal(60)])
        unchanged_raw = Decimal(2)

        result = decompose_leg_delta(
            prior_z=prior_cross.z_for(unchanged_raw),
            current_z=current_cross.z_for(unchanged_raw),
            prior_raw=unchanged_raw,
            current_cross_section=current_cross,
        )

        assert result.own_evidence_effect == Decimal(0)
        assert result.cohort_distribution_effect == result.delta_z

    def test_winsorised_endpoints_still_add_up(self):
        """The reported z is winsorised, so the counterfactual must be too."""

        cross = _cross_section([Decimal(0), Decimal(1), Decimal(2), Decimal(3)])
        prior_raw = Decimal(1)
        current_raw = Decimal(10_000)  # far outside +-2.5 sigma

        result = decompose_leg_delta(
            prior_z=cross.z_for(prior_raw),
            current_z=cross.z_for(current_raw),
            prior_raw=prior_raw,
            current_cross_section=cross,
        )

        assert cross.z_for(current_raw) == Decimal("2.5")  # clipped at the winsor bound
        assert result.own_evidence_effect + result.cohort_distribution_effect == result.delta_z


class TestFailClosed:
    def test_no_prior_row_yields_no_attribution_at_all(self):
        cross = _cross_section([Decimal(1), Decimal(2), Decimal(3)])
        result = decompose_leg_delta(
            prior_z=None,
            current_z=Decimal("0.5"),
            prior_raw=None,
            current_cross_section=cross,
        )
        assert result.delta_z is None
        assert result.own_evidence_effect is None
        assert result.cohort_distribution_effect is None
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_PRIOR

    def test_a_leg_that_lost_its_evidence_today_is_reported_distinctly(self):
        """"The leg is non-computable today" and "this leg has no history" are
        different facts about the row; one reason for both forces a guess."""

        cross = _cross_section([Decimal(1), Decimal(2), Decimal(3)])
        result = decompose_leg_delta(
            prior_z=Decimal("0.5"),
            current_z=None,
            prior_raw=Decimal(2),
            current_cross_section=cross,
        )
        assert result.delta_z is None
        assert result.own_evidence_effect is None
        assert result.cohort_distribution_effect is None
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_CURRENT

    def test_a_leg_missing_on_both_days_reports_the_current_gap(self):
        result = decompose_leg_delta(
            prior_z=None,
            current_z=None,
            prior_raw=None,
            current_cross_section=None,
        )
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_CURRENT

    def test_a_prior_z_without_a_prior_raw_reports_the_delta_but_no_split(self):
        """Never silently charge the whole move to own evidence."""

        cross = _cross_section([Decimal(1), Decimal(2), Decimal(3)])
        result = decompose_leg_delta(
            prior_z=Decimal("-1"),
            current_z=Decimal("1"),
            prior_raw=None,
            current_cross_section=cross,
        )
        assert result.delta_z == Decimal(2)
        assert result.own_evidence_effect is None
        assert result.cohort_distribution_effect is None
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_PRIOR

    def test_a_degenerate_current_cross_section_reports_the_delta_but_no_split(self):
        constant = _cross_section([Decimal(5), Decimal(5), Decimal(5)])
        result = decompose_leg_delta(
            prior_z=Decimal("-1"),
            current_z=Decimal("1"),
            prior_raw=Decimal(5),
            current_cross_section=constant,
        )
        assert result.delta_z == Decimal(2)
        assert result.own_evidence_effect is None
        assert result.cohort_distribution_effect is None
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_CROSS_SECTION

    def test_a_missing_cross_section_reports_the_delta_but_no_split(self):
        result = decompose_leg_delta(
            prior_z=Decimal("-1"),
            current_z=Decimal("1"),
            prior_raw=Decimal(5),
            current_cross_section=None,
        )
        assert result.delta_z == Decimal(2)
        assert result.own_evidence_effect is None
        assert result.cohort_distribution_effect is None
        assert result.reason_unavailable == REASON_DECOMPOSITION_NO_CROSS_SECTION


class TestDecompositionProperties:
    """The identity has to hold for arbitrary inputs, not just chosen ones."""

    def test_effects_sum_to_delta_across_generated_cross_sections(self):
        rng = random.Random(SEED)
        checked = 0
        for _ in range(CASES):
            prior_values = [Decimal(rng.randint(-400, 400)) / Decimal(100) for _ in range(rng.randint(2, 12))]
            current_values = [Decimal(rng.randint(-400, 400)) / Decimal(100) for _ in range(rng.randint(2, 12))]
            prior_raw = Decimal(rng.randint(-400, 400)) / Decimal(100)
            current_raw = Decimal(rng.randint(-400, 400)) / Decimal(100)

            prior_cross = _cross_section(prior_values)
            current_cross = _cross_section(current_values)
            prior_z = prior_cross.z_for(prior_raw)
            current_z = current_cross.z_for(current_raw)
            if prior_z is None or current_z is None:
                continue

            result = decompose_leg_delta(
                prior_z=prior_z,
                current_z=current_z,
                prior_raw=prior_raw,
                current_cross_section=current_cross,
            )
            assert result.own_evidence_effect is not None
            assert result.cohort_distribution_effect is not None
            assert result.own_evidence_effect + result.cohort_distribution_effect == result.delta_z
            checked += 1
        assert checked > CASES // 2  # the generator actually produced usable cases

    def test_a_multi_tier_blend_decomposes_exactly_too(self):
        rng = random.Random(SEED + 1)
        for _ in range(CASES):
            cross = LegCrossSection(
                cohort_values=tuple(Decimal(rng.randint(-200, 200)) / Decimal(100) for _ in range(4)),
                parent_values=tuple(Decimal(rng.randint(-200, 200)) / Decimal(100) for _ in range(9)),
                universe_values=tuple(Decimal(rng.randint(-200, 200)) / Decimal(100) for _ in range(20)),
                lambda_cohort=Decimal("0.25"),
                lambda_parent=Decimal("0.6"),
            )
            prior_raw = Decimal(rng.randint(-200, 200)) / Decimal(100)
            current_raw = Decimal(rng.randint(-200, 200)) / Decimal(100)
            prior_z = cross.z_for(prior_raw)
            current_z = cross.z_for(current_raw)
            if prior_z is None or current_z is None:
                continue
            result = decompose_leg_delta(
                prior_z=prior_z,
                current_z=current_z,
                prior_raw=prior_raw,
                current_cross_section=cross,
            )
            # Same cross-section on both days -> the cohort explains nothing.
            assert result.cohort_distribution_effect == Decimal(0)
            assert result.own_evidence_effect == result.delta_z
