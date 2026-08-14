"""Deterministic gate cascade producing BUY/ADD/HOLD/TRIM/SELL (arc42 §5.6).

The two HOLD states are distinct and must never be conflated: HOLD_NO_ACTION
means every gate was evaluated and none triggered; HOLD_INSUFFICIENT_DATA
means coverage or cohort confidence was too low to trust the evaluation at
all, and is checked first, short-circuiting the rest of the cascade.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import Action, CohortConfidence
from auspex.models.policy import GateResult
from auspex.policy.gates import (
    PolicyContext,
    gate_cash_after_trade_min,
    gate_cohort_confidence_min,
    gate_cohort_confidence_not_low,
    gate_consecutive_weakening_min,
    gate_cost_pct_max,
    gate_coverage_min,
    gate_held,
    gate_not_held,
    gate_percentile_max,
    gate_percentile_min,
    gate_resulting_weight_max,
    gate_thesis_linkage_z_max,
    gate_trade_min,
    gate_valuation_brake_z_min,
    gate_weakening,
    gate_weight_gap_min,
    gate_weight_max,
)


@dataclass(frozen=True)
class PolicyThresholds:
    min_coverage_for_buy: Decimal
    buy_min_percentile: int
    buy_min_cohort_confidence: CohortConfidence
    buy_min_valuation_brake_z: Decimal
    buy_max_resulting_weight_pct: Decimal
    buy_min_cash_after_trade_chf: Decimal
    buy_max_cost_pct_of_trade: Decimal
    buy_min_trade_chf: Decimal
    add_min_percentile: int
    add_min_weight_gap_pct: Decimal
    trim_max_weight_pct: Decimal
    trim_max_percentile_weakening: int
    sell_max_percentile: int
    sell_min_consecutive_weakening_sessions: int
    sell_max_thesis_linkage_z: Decimal
    target_weight_max_pct: Decimal
    target_weight_floor_pct: Decimal


def load_policy_thresholds(
    policy_config: dict,
    *,
    risk_profile: str = "MODERATE",
    cash_reserve_chf: str | None = None,
) -> PolicyThresholds:
    profile = policy_config.get("risk_profiles", {}).get(risk_profile.upper())
    if profile is None:
        profile = policy_config.get("risk_profiles", {}).get("MODERATE", {})
    buy = policy_config["buy"]
    add = policy_config["add"]
    trim = policy_config["trim"]
    sell = policy_config["sell"]
    target = policy_config["target_weight"]
    return PolicyThresholds(
        min_coverage_for_buy=Decimal(
            profile.get(
                "buy_min_coverage",
                policy_config["coverage"]["minimum_for_buy"],
            )
        ),
        buy_min_percentile=profile.get("buy_min_percentile", buy["min_percentile"]),
        buy_min_cohort_confidence=CohortConfidence(
            profile.get(
                "buy_min_cohort_confidence",
                buy["min_cohort_confidence"],
            )
        ),
        buy_min_valuation_brake_z=Decimal(
            profile.get("buy_min_valuation_brake_z", buy["min_valuation_brake_z"])
        ),
        buy_max_resulting_weight_pct=Decimal(
            profile.get(
                "max_resulting_weight_pct",
                buy["max_resulting_weight_pct"],
            )
        ),
        buy_min_cash_after_trade_chf=Decimal(
            cash_reserve_chf
            or profile.get(
                "default_cash_reserve_chf",
                buy["min_cash_after_trade_chf"],
            )
        ),
        buy_max_cost_pct_of_trade=Decimal(
            profile.get("max_cost_pct_of_trade", buy["max_cost_pct_of_trade"])
        ),
        buy_min_trade_chf=Decimal(
            profile.get("min_trade_chf", buy["min_trade_chf"])
        ),
        add_min_percentile=profile.get("add_min_percentile", add["min_percentile"]),
        add_min_weight_gap_pct=Decimal(
            profile.get("add_min_weight_gap_pct", add["min_weight_gap_pct"])
        ),
        trim_max_weight_pct=Decimal(
            profile.get("trim_max_weight_pct", trim["max_weight_pct"])
        ),
        trim_max_percentile_weakening=profile.get(
            "trim_max_percentile_weakening",
            trim["max_percentile_weakening"],
        ),
        sell_max_percentile=profile.get(
            "sell_max_percentile",
            sell["max_percentile"],
        ),
        sell_min_consecutive_weakening_sessions=profile.get(
            "sell_min_consecutive_weakening_sessions",
            sell["min_consecutive_weakening_sessions"],
        ),
        sell_max_thesis_linkage_z=Decimal(
            profile.get("sell_max_thesis_linkage_z", sell["max_thesis_linkage_z"])
        ),
        target_weight_max_pct=Decimal(
            profile.get("target_weight_max_pct", target["max_pct"])
        ),
        target_weight_floor_pct=Decimal(
            profile.get("target_weight_floor_pct", target["floor_pct"])
        ),
    )


def evaluate_action(ctx: PolicyContext, t: PolicyThresholds) -> tuple[Action, list[GateResult]]:
    trace: list[GateResult] = []

    coverage_gate = gate_coverage_min(ctx, t.min_coverage_for_buy)
    confidence_gate = gate_cohort_confidence_not_low(ctx)
    trace.extend([coverage_gate, confidence_gate])
    if not (coverage_gate.passed and confidence_gate.passed):
        return Action.HOLD_INSUFFICIENT_DATA, trace

    if not ctx.held:
        buy_gates = [
            gate_not_held(ctx),
            gate_percentile_min(ctx, t.buy_min_percentile),
            gate_coverage_min(ctx, t.min_coverage_for_buy),
            gate_cohort_confidence_min(ctx, t.buy_min_cohort_confidence),
            gate_valuation_brake_z_min(ctx, t.buy_min_valuation_brake_z),
            gate_resulting_weight_max(ctx, t.buy_max_resulting_weight_pct),
            gate_cash_after_trade_min(ctx, t.buy_min_cash_after_trade_chf),
            gate_cost_pct_max(ctx, t.buy_max_cost_pct_of_trade),
            gate_trade_min(ctx, t.buy_min_trade_chf),
        ]
        trace.extend(buy_gates)
        if all(g.passed for g in buy_gates):
            return Action.BUY, trace
        return Action.HOLD_NO_ACTION, trace

    # held: ADD, then TRIM(overweight)/SELL/TRIM(weakness) in that priority order.
    #
    # SELL's criteria (percentile < 25 AND weakening >= 10 sessions AND thesis
    # broken) are strictly narrower than TRIM's weakness criteria (percentile <
    # 40 AND weakening) whenever both hold, so SELL is evaluated first — it is
    # the more severe, more specific response to sustained deterioration. The
    # overweight TRIM trigger (weight > 15%) is independent of conviction and
    # is checked immediately after ADD, ahead of SELL, since a position that is
    # both overweight and deteriorating should be trimmed for size discipline
    # regardless of how severe the deterioration is.
    add_gates = [
        gate_held(ctx),
        gate_percentile_min(ctx, t.add_min_percentile),
        gate_weight_gap_min(ctx, t.add_min_weight_gap_pct),
        gate_cash_after_trade_min(ctx, t.buy_min_cash_after_trade_chf),
        gate_cost_pct_max(ctx, t.buy_max_cost_pct_of_trade),
        gate_trade_min(ctx, t.buy_min_trade_chf),
    ]
    trace.extend(add_gates)
    if all(g.passed for g in add_gates):
        return Action.ADD, trace

    trim_weight_gate = gate_weight_max(ctx, t.trim_max_weight_pct)
    trace.append(trim_weight_gate)
    if trim_weight_gate.passed:
        return Action.TRIM, trace

    sell_gates = [
        gate_percentile_max(ctx, t.sell_max_percentile),
        gate_consecutive_weakening_min(ctx, t.sell_min_consecutive_weakening_sessions),
        gate_thesis_linkage_z_max(ctx, t.sell_max_thesis_linkage_z),
    ]
    trace.extend(sell_gates)
    if all(g.passed for g in sell_gates):
        return Action.SELL, trace

    trim_percentile_gate = gate_percentile_max(ctx, t.trim_max_percentile_weakening)
    trim_weakening_gate = gate_weakening(ctx)
    trace.extend([trim_percentile_gate, trim_weakening_gate])
    if trim_percentile_gate.passed and trim_weakening_gate.passed:
        return Action.TRIM, trace

    return Action.HOLD_NO_ACTION, trace
