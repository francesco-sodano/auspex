"""Individual policy gates (arc42 §5.6). Each gate is a pure predicate over a
:class:`PolicyContext`, returning a :class:`~auspex.models.policy.GateResult`
so every outcome — pass or fail — is recorded with its actual and threshold
value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import CohortConfidence, Direction
from auspex.models.policy import GateResult

_CONFIDENCE_RANK = {CohortConfidence.LOW: 0, CohortConfidence.MEDIUM: 1, CohortConfidence.HIGH: 2}


@dataclass(frozen=True)
class PolicyContext:
    security_id: str
    held: bool
    percentile: int | None
    coverage: Decimal
    cohort_confidence: CohortConfidence
    valuation_brake_z: Decimal | None
    thesis_linkage_z: Decimal | None
    direction: Direction
    consecutive_weakening_sessions: int
    current_weight_pct: Decimal
    target_weight_pct: Decimal
    resulting_weight_pct: Decimal
    cash_after_trade_chf: Decimal
    estimated_cost_chf: Decimal
    trade_notional_chf: Decimal


def _gate(name: str, passed: bool, actual, threshold, detail: str | None = None) -> GateResult:
    return GateResult(
        gate=name,
        passed=passed,
        actual_value=None if actual is None else str(actual),
        threshold_value=None if threshold is None else str(threshold),
        detail=detail,
    )


def gate_not_held(ctx: PolicyContext) -> GateResult:
    return _gate("not_held", not ctx.held, ctx.held, False)


def gate_held(ctx: PolicyContext) -> GateResult:
    return _gate("held", ctx.held, ctx.held, True)


def gate_percentile_min(ctx: PolicyContext, threshold: int) -> GateResult:
    passed = ctx.percentile is not None and ctx.percentile >= threshold
    return _gate("percentile_min", passed, ctx.percentile, threshold)


def gate_percentile_max(ctx: PolicyContext, threshold: int) -> GateResult:
    passed = ctx.percentile is not None and ctx.percentile < threshold
    return _gate("percentile_max", passed, ctx.percentile, threshold)


def gate_coverage_min(ctx: PolicyContext, threshold: Decimal) -> GateResult:
    return _gate("coverage_min", ctx.coverage >= threshold, ctx.coverage, threshold)


def gate_cohort_confidence_min(ctx: PolicyContext, threshold: CohortConfidence) -> GateResult:
    passed = _CONFIDENCE_RANK[ctx.cohort_confidence] >= _CONFIDENCE_RANK[threshold]
    return _gate("cohort_confidence_min", passed, ctx.cohort_confidence, threshold)


def gate_cohort_confidence_not_low(ctx: PolicyContext) -> GateResult:
    passed = ctx.cohort_confidence != CohortConfidence.LOW
    return _gate("cohort_confidence_not_low", passed, ctx.cohort_confidence, "not LOW")


def gate_valuation_brake_z_min(ctx: PolicyContext, threshold: Decimal) -> GateResult:
    passed = ctx.valuation_brake_z is not None and ctx.valuation_brake_z >= threshold
    return _gate("valuation_brake_z_min", passed, ctx.valuation_brake_z, threshold)


def gate_thesis_linkage_z_max(ctx: PolicyContext, threshold: Decimal) -> GateResult:
    passed = ctx.thesis_linkage_z is not None and ctx.thesis_linkage_z < threshold
    return _gate("thesis_linkage_z_max", passed, ctx.thesis_linkage_z, threshold)


def gate_resulting_weight_max(ctx: PolicyContext, threshold_pct: Decimal) -> GateResult:
    return _gate(
        "resulting_weight_max", ctx.resulting_weight_pct <= threshold_pct, ctx.resulting_weight_pct, threshold_pct
    )


def gate_cash_after_trade_min(ctx: PolicyContext, threshold_chf: Decimal) -> GateResult:
    return _gate(
        "cash_after_trade_min", ctx.cash_after_trade_chf >= threshold_chf, ctx.cash_after_trade_chf, threshold_chf
    )


def gate_cost_pct_max(ctx: PolicyContext, threshold_pct: Decimal) -> GateResult:
    if ctx.trade_notional_chf == 0:
        return _gate("cost_pct_max", False, None, threshold_pct, "zero trade notional")
    cost_pct = ctx.estimated_cost_chf / ctx.trade_notional_chf
    return _gate("cost_pct_max", cost_pct <= threshold_pct, cost_pct, threshold_pct)


def gate_trade_min(ctx: PolicyContext, threshold_chf: Decimal) -> GateResult:
    return _gate("trade_min", ctx.trade_notional_chf >= threshold_chf, ctx.trade_notional_chf, threshold_chf)


def gate_weight_gap_min(ctx: PolicyContext, threshold_pp: Decimal) -> GateResult:
    gap = ctx.target_weight_pct - ctx.current_weight_pct
    return _gate("weight_gap_min", gap >= threshold_pp, gap, threshold_pp)


def gate_weight_max(ctx: PolicyContext, threshold_pct: Decimal) -> GateResult:
    return _gate("weight_max", ctx.current_weight_pct > threshold_pct, ctx.current_weight_pct, threshold_pct)


def gate_weakening(ctx: PolicyContext) -> GateResult:
    return _gate("direction_weakening", ctx.direction == Direction.WEAKENING, ctx.direction, Direction.WEAKENING)


def gate_consecutive_weakening_min(ctx: PolicyContext, threshold_sessions: int) -> GateResult:
    passed = ctx.direction == Direction.WEAKENING and ctx.consecutive_weakening_sessions >= threshold_sessions
    return _gate(
        "consecutive_weakening_min",
        passed,
        ctx.consecutive_weakening_sessions,
        threshold_sessions,
    )
