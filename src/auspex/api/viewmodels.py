"""Shared read-model assembly helpers for API routes (arc42 §11, §12).

`briefing.py` and `securities.py` both need to turn a persisted
`auspex.models.policy.Recommendation`/`GateResult` into the frontend-facing
`RecommendationOut`/`GateTraceOut` shape, and both need a run status mapped
onto the frontend's narrower literal union. Centralised here so the two
routes stay consistent instead of drifting.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from auspex.api.schemas import BriefingRunStatus, GateTraceOut, RecommendationOut
from auspex.models.common import as_decimal
from auspex.models.enums import RunStatus
from auspex.models.policy import GateResult, Recommendation
from auspex.models.scoring import ScoreSnapshot


def map_run_status(status: RunStatus) -> BriefingRunStatus:
    """`RunStatus` has a `TIMEOUT` value the frontend union doesn't; a timed-out
    run is, from the owner's perspective, a failed one."""

    if status == RunStatus.TIMEOUT:
        return "FAILED"
    return status.value  # type: ignore[return-value]


def gate_trace_out(gate_trace: list[GateResult]) -> list[GateTraceOut]:
    return [
        GateTraceOut(gate=g.gate, passed=g.passed, actual=g.actual_value, threshold=g.threshold_value, reason=g.detail)
        for g in gate_trace
    ]


def _decimal_text(value: str | None, *, multiplier: Decimal = Decimal(1)) -> str:
    if value is None:
        return "missing"
    try:
        number = Decimal(value) * multiplier
    except InvalidOperation:
        return value
    rendered = f"{number:.2f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _gate_value_text(gate_name: str, value: str | None) -> str:
    if value is None:
        return "missing"
    if gate_name in {"cash_after_trade_min", "trade_min"}:
        return f"CHF {_decimal_text(value)}"
    if gate_name in {"coverage_min", "cost_pct_max"}:
        return f"{_decimal_text(value, multiplier=Decimal(100))}%"
    if gate_name in {"resulting_weight_max", "weight_gap_min", "weight_max"}:
        return f"{_decimal_text(value)}%"
    if gate_name in {"percentile_min", "percentile_max", "consecutive_weakening_min"}:
        return _decimal_text(value)
    if gate_name in {"valuation_brake_z_min", "thesis_linkage_z_max"}:
        return _decimal_text(value)
    return value


def build_rationale(score: ScoreSnapshot | None, recommendation: Recommendation) -> str:
    """Prefer the security's own generated narrative; fall back to a
    deterministic gate-cascade summary when no narrative has been written yet."""

    if score is not None and score.narrative:
        return score.narrative
    passed = sum(1 for gate in recommendation.gate_trace if gate.passed)
    total = len(recommendation.gate_trace)
    action_label = recommendation.action.value.replace("_", " ").title()
    return f"{action_label} — {passed}/{total} policy gates passed."


def build_recommendation_out(
    recommendation: Recommendation, ticker: str, company_name: str, score: ScoreSnapshot | None
) -> RecommendationOut:
    failed = [gate for gate in recommendation.gate_trace if not gate.passed]

    def blocker(gate: GateResult) -> str:
        labels = {
            "cash_after_trade_min": "Cash reserve",
            "trade_min": "Minimum trade size",
            "valuation_brake_z_min": "Valuation",
            "coverage_min": "Data coverage",
            "cohort_confidence_min": "Cohort confidence",
            "resulting_weight_max": "Position-size limit",
            "cost_pct_max": "Transaction cost",
            "percentile_min": "Auspex Score",
        }
        label = labels.get(gate.gate, gate.gate.replace("_", " ").title())
        if gate.gate == "cash_after_trade_min":
            return (
                f"{label}: projected {_gate_value_text(gate.gate, gate.actual_value)} remaining; "
                f"minimum {_gate_value_text(gate.gate, gate.threshold_value)}"
            )
        return (
            f"{label}: {_gate_value_text(gate.gate, gate.actual_value)}; "
            f"required {_gate_value_text(gate.gate, gate.threshold_value)}"
        )

    blocking_reasons = [
        blocker(gate)
        for gate in failed
        if gate.gate
        not in {"held", "not_held", "cohort_confidence_not_low"}
    ]
    buy_ready = recommendation.action.value in {"BUY", "ADD"}
    return RecommendationOut(
        id=recommendation.id,
        security_id=recommendation.security_id,
        ticker=ticker,
        company_name=company_name,
        action=recommendation.action,
        rationale=build_rationale(score, recommendation),
        target_weight=recommendation.target_weight_pct,
        current_weight=recommendation.current_weight_pct,
        suggested_trade_chf=recommendation.suggested_trade_chf,
        suggested_quantity=recommendation.suggested_quantity,
        allocation_mode=recommendation.allocation_mode,
        allocation_trace=gate_trace_out(recommendation.allocation_trace),
        estimated_cost_chf=(recommendation.cost_overlay.estimated_cost_chf if recommendation.cost_overlay else None),
        auspex_score=score.percentile if score is not None else None,
        buy_ready=buy_ready,
        blocking_reasons=blocking_reasons,
        gate_trace=gate_trace_out(recommendation.gate_trace),
    )


def contribution_delta(delta_z: str | None, weight: str | None) -> str:
    """`weight * delta_z` when a current leg weight is known, else `delta_z`
    verbatim, else `"0"` — arc42 §12 "ranked by absolute contribution delta"."""

    if delta_z is None:
        return "0"
    if weight is None:
        return delta_z
    return str(as_decimal(weight) * as_decimal(delta_z))


def sum_decimal_strings(values: list[str | None]) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is not None:
            total += as_decimal(value)
    return total
