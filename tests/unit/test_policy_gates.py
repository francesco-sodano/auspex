"""Unit tests for the deterministic policy gate cascade (arc42 §5.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from auspex.config import load_policy
from auspex.models.enums import Action, CohortConfidence, Direction
from auspex.policy.assertions import run_post_run_assertions
from auspex.policy.engine import evaluate_action, load_policy_thresholds
from auspex.policy.gates import PolicyContext
from auspex.policy.target_weight import target_weight_pct


@pytest.fixture(scope="module")
def thresholds():
    return load_policy_thresholds(load_policy())


def make_context(**overrides) -> PolicyContext:
    defaults = dict(
        security_id="sec-1",
        held=False,
        percentile=50,
        coverage=Decimal("1.0"),
        cohort_confidence=CohortConfidence.HIGH,
        valuation_brake_z=Decimal("0.0"),
        thesis_linkage_z=Decimal("0.0"),
        direction=Direction.STABLE,
        consecutive_weakening_sessions=0,
        current_weight_pct=Decimal("0"),
        target_weight_pct=Decimal("7.5"),
        resulting_weight_pct=Decimal("7.5"),
        cash_after_trade_chf=Decimal("5000"),
        estimated_cost_chf=Decimal("10"),
        trade_notional_chf=Decimal("3000"),
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


class TestHoldInsufficientData:
    def test_low_coverage_short_circuits_to_insufficient_data(self, thresholds):
        ctx = make_context(coverage=Decimal("0.5"), percentile=90)
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_INSUFFICIENT_DATA
        assert any(g.gate == "coverage_min" and not g.passed for g in trace)

    def test_low_cohort_confidence_short_circuits(self, thresholds):
        ctx = make_context(cohort_confidence=CohortConfidence.LOW, percentile=90)
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_INSUFFICIENT_DATA

    def test_never_conflated_with_hold_no_action(self, thresholds):
        insufficient_ctx = make_context(coverage=Decimal("0.5"))
        no_action_ctx = make_context(coverage=Decimal("1.0"), percentile=50)
        insufficient_action, _ = evaluate_action(insufficient_ctx, thresholds)
        no_action, _ = evaluate_action(no_action_ctx, thresholds)
        assert insufficient_action == Action.HOLD_INSUFFICIENT_DATA
        assert no_action == Action.HOLD_NO_ACTION
        assert insufficient_action != no_action


class TestBuy:
    def test_all_gates_pass_yields_buy(self, thresholds):
        ctx = make_context(held=False, percentile=80, valuation_brake_z=Decimal("0.5"))
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.BUY
        assert all(g.passed for g in trace)

    def test_percentile_below_75_blocks_buy(self, thresholds):
        ctx = make_context(held=False, percentile=74)
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION

    def test_valuation_brake_below_threshold_blocks_buy(self, thresholds):
        ctx = make_context(held=False, percentile=80, valuation_brake_z=Decimal("-2.0"))
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION
        assert any(g.gate == "valuation_brake_z_min" and not g.passed for g in trace)

    def test_resulting_weight_over_15pct_blocks_buy(self, thresholds):
        ctx = make_context(held=False, percentile=90, resulting_weight_pct=Decimal("16"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION

    def test_insufficient_cash_after_trade_blocks_buy(self, thresholds):
        ctx = make_context(held=False, percentile=90, cash_after_trade_chf=Decimal("1000"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION

    def test_cost_pct_over_1pct_blocks_buy(self, thresholds):
        ctx = make_context(
            held=False, percentile=90, estimated_cost_chf=Decimal("500"), trade_notional_chf=Decimal("3000")
        )
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION

    def test_trade_below_2000chf_blocks_buy(self, thresholds):
        ctx = make_context(held=False, percentile=90, trade_notional_chf=Decimal("1500"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION

    def test_already_held_never_buys(self, thresholds):
        ctx = make_context(held=True, percentile=95)
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.BUY


class TestAdd:
    def test_all_gates_pass_yields_add(self, thresholds):
        ctx = make_context(
            held=True, percentile=75, current_weight_pct=Decimal("5"), target_weight_pct=Decimal("11.25")
        )
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.ADD

    def test_percentile_below_70_blocks_add(self, thresholds):
        ctx = make_context(held=True, percentile=69, current_weight_pct=Decimal("5"), target_weight_pct=Decimal("11"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.ADD

    def test_weight_gap_below_3pp_blocks_add(self, thresholds):
        ctx = make_context(held=True, percentile=80, current_weight_pct=Decimal("11"), target_weight_pct=Decimal("12"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.ADD


class TestTrim:
    def test_overweight_triggers_trim(self, thresholds):
        ctx = make_context(held=True, percentile=60, current_weight_pct=Decimal("16"), direction=Direction.STABLE)
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.TRIM
        assert any(g.gate == "weight_max" and g.passed for g in trace)

    def test_weak_low_percentile_triggers_trim(self, thresholds):
        ctx = make_context(held=True, percentile=35, direction=Direction.WEAKENING, current_weight_pct=Decimal("8"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action == Action.TRIM

    def test_low_percentile_but_not_weakening_does_not_trim(self, thresholds):
        ctx = make_context(held=True, percentile=35, direction=Direction.STABLE, current_weight_pct=Decimal("8"))
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.TRIM


class TestSell:
    def test_all_conditions_trigger_sell(self, thresholds):
        ctx = make_context(
            held=True,
            percentile=10,
            direction=Direction.WEAKENING,
            consecutive_weakening_sessions=12,
            thesis_linkage_z=Decimal("-1.5"),
            current_weight_pct=Decimal("8"),
        )
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.SELL

    def test_insufficient_consecutive_sessions_blocks_sell(self, thresholds):
        ctx = make_context(
            held=True,
            percentile=10,
            direction=Direction.WEAKENING,
            consecutive_weakening_sessions=3,
            thesis_linkage_z=Decimal("-1.5"),
            current_weight_pct=Decimal("8"),
        )
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.SELL

    def test_percentile_above_25_blocks_sell(self, thresholds):
        ctx = make_context(
            held=True,
            percentile=30,
            direction=Direction.WEAKENING,
            consecutive_weakening_sessions=12,
            thesis_linkage_z=Decimal("-1.5"),
            current_weight_pct=Decimal("8"),
        )
        action, _ = evaluate_action(ctx, thresholds)
        assert action != Action.SELL


class TestHoldNoAction:
    def test_nothing_triggers_yields_hold_no_action(self, thresholds):
        ctx = make_context(held=True, percentile=50, direction=Direction.STABLE, current_weight_pct=Decimal("7"))
        action, trace = evaluate_action(ctx, thresholds)
        assert action == Action.HOLD_NO_ACTION
        assert len(trace) > 0  # every gate evaluated and recorded


class TestTargetWeight:
    def test_80th_percentile_targets_12pct(self):
        assert target_weight_pct(80) == Decimal("12")

    def test_50th_percentile_targets_7_5pct(self):
        assert target_weight_pct(50) == Decimal("7.5")

    def test_floored_at_4pct(self):
        assert target_weight_pct(10) == Decimal("4")

    def test_none_percentile_floors(self):
        assert target_weight_pct(None) == Decimal("4")

    def test_100th_percentile_targets_15pct(self):
        assert target_weight_pct(100) == Decimal("15")


class TestPostRunAssertions:
    def test_no_violations_when_healthy(self):
        actions = [Action.BUY] + [Action.HOLD_NO_ACTION] * 90
        violations = run_post_run_assertions(
            actions=actions, scored_security_count=91, eligible_but_no_cash_count=0, policy_config=load_policy()
        )
        assert violations == []

    def test_violation_when_no_buy_and_no_eligible(self):
        actions = [Action.HOLD_NO_ACTION] * 91
        violations = run_post_run_assertions(
            actions=actions, scored_security_count=91, eligible_but_no_cash_count=0, policy_config=load_policy()
        )
        names = {v.name for v in violations}
        assert "at_least_one_actionable_or_eligible_no_cash" in names

    def test_violation_when_too_many_hold_insufficient_data(self):
        actions = [Action.HOLD_INSUFFICIENT_DATA] * 50 + [Action.BUY] * 5
        violations = run_post_run_assertions(
            actions=actions, scored_security_count=55, eligible_but_no_cash_count=0, policy_config=load_policy()
        )
        names = {v.name for v in violations}
        assert "hold_insufficient_data_fraction_below_max" in names

    def test_violation_when_too_few_scored(self):
        violations = run_post_run_assertions(
            actions=[Action.BUY], scored_security_count=10, eligible_but_no_cash_count=0, policy_config=load_policy()
        )
        names = {v.name for v in violations}
        assert "min_scored_securities" in names

    def test_eligible_but_no_cash_satisfies_buy_assertion(self):
        actions = [Action.HOLD_NO_ACTION] * 91
        violations = run_post_run_assertions(
            actions=actions, scored_security_count=91, eligible_but_no_cash_count=1, policy_config=load_policy()
        )
        names = {v.name for v in violations}
        assert "at_least_one_actionable_or_eligible_no_cash" not in names

    def test_trim_counts_as_actionable(self):
        actions = [Action.TRIM] + [Action.HOLD_NO_ACTION] * 90
        violations = run_post_run_assertions(
            actions=actions,
            scored_security_count=91,
            eligible_but_no_cash_count=0,
            policy_config=load_policy(),
        )
        names = {violation.name for violation in violations}
        assert "at_least_one_actionable_or_eligible_no_cash" not in names
