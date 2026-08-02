from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from .costs import estimate_costs
from .risk_profile import policy_for_profile


MODEL_VERSION = "e15_v1"
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CandidateSignal:
    security_sk: int
    ticker: str
    opportunity_score: Decimal
    coverage_status: str
    current_value_base: Decimal
    current_weight: Decimal
    country: str | None
    spread_bps: Decimal = Decimal("5")
    theme_id: str | None = None
    coverage_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioContext:
    total_value_base: Decimal
    cash_base: Decimal
    risk_profile: str
    base_currency: str
    annual_trade_count: int = 0


@dataclass(frozen=True)
class Recommendation:
    recommendation_id: str
    security_sk: int
    ticker: str
    action: str
    current_weight: Decimal
    target_weight: Decimal
    suggested_amount_base: Decimal
    estimated_cost_base: Decimal
    expected_edge_base: Decimal
    confidence: str
    rationale: str
    suppression_reasons: tuple[str, ...]
    tax_flags: tuple[str, ...]
    as_of: str
    model_version: str = MODEL_VERSION


def _target_weight(score: Decimal, cap: Decimal) -> Decimal:
    if score >= 80:
        return cap
    if score >= 70:
        return cap * Decimal("0.75")
    if score >= 60:
        return cap * Decimal("0.50")
    return Decimal("0")


def _recommendation_id(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _edge_rate(action: str, score: Decimal, overweight: bool) -> Decimal:
    if overweight and action == "TRIM":
        return Decimal("0.10")
    if action in {"BUY", "ADD"}:
        return max(Decimal("0"), (score - Decimal("60")) / Decimal("100"))
    return max(Decimal("0"), (Decimal("50") - score) / Decimal("100"))


def build_recommendations(
    portfolio: PortfolioContext,
    candidates: list[CandidateSignal],
    *,
    as_of: str,
) -> list[Recommendation]:
    if portfolio.total_value_base <= 0:
        raise ValueError("portfolio total value must be positive")
    if portfolio.cash_base < 0:
        raise ValueError("portfolio cash cannot be negative")
    if portfolio.annual_trade_count < 0:
        raise ValueError("annual_trade_count cannot be negative")
    policy = policy_for_profile(portfolio.risk_profile)
    cash_buffer = portfolio.total_value_base * policy.cash_buffer_pct
    available_cash = max(Decimal("0"), portfolio.cash_base - cash_buffer)
    ordered = sorted(
        candidates,
        key=lambda row: (-row.opportunity_score, row.ticker, row.security_sk),
    )
    recommendations: list[Recommendation] = []
    projected_trades = portfolio.annual_trade_count

    for candidate in ordered:
        if not Decimal("0") <= candidate.current_weight <= Decimal("1"):
            raise ValueError("current_weight must be between 0 and 1")
        if not Decimal("0") <= candidate.opportunity_score <= Decimal("100"):
            raise ValueError("opportunity_score must be between 0 and 100")

        overweight = candidate.current_weight > policy.max_position_weight
        suppression_reasons: list[str] = []
        proposed_target = candidate.current_weight
        if overweight:
            proposed_target = policy.max_position_weight
        elif candidate.coverage_status != "READY":
            suppression_reasons.append("coverage")
        elif candidate.current_value_base > 0 and candidate.opportunity_score < 45:
            proposed_target = Decimal("0")
        else:
            proposed_target = max(
                candidate.current_weight,
                _target_weight(candidate.opportunity_score, policy.max_position_weight),
            )

        delta = (proposed_target - candidate.current_weight) * portfolio.total_value_base
        action = "HOLD"
        if delta > 0:
            action = "ADD" if candidate.current_value_base > 0 else "BUY"
        elif delta < 0:
            action = "SELL" if proposed_target == 0 else "TRIM"

        notional = abs(delta)
        if action in {"BUY", "ADD"}:
            notional = min(notional, available_cash)
        costs = estimate_costs(
            notional,
            security_country=candidate.country,
            spread_bps=candidate.spread_bps,
        )
        if action in {"BUY", "ADD"} and notional + costs.total_base > available_cash:
            notional = max(Decimal("0"), available_cash - costs.total_base)
            costs = estimate_costs(
                notional,
                security_country=candidate.country,
                spread_bps=candidate.spread_bps,
            )
        expected_edge = notional * _edge_rate(action, candidate.opportunity_score, overweight)

        if action != "HOLD" and notional < policy.min_trade_base:
            suppression_reasons.append("minimum_trade")
        if action != "HOLD" and expected_edge <= costs.total_base:
            suppression_reasons.append("cost_exceeds_edge")
        if suppression_reasons:
            action = "HOLD"
            proposed_target = candidate.current_weight
            signed_amount = Decimal("0")
        else:
            signed_amount = notional if action in {"BUY", "ADD"} else -notional
            if action in {"BUY", "ADD"}:
                available_cash -= notional + costs.total_base
            projected_trades += int(action != "HOLD")

        tax_flags: list[str] = []
        if action != "HOLD" and projected_trades >= 24:
            tax_flags.append("swiss_professional_securities_dealer_review")
        confidence = (
            "LOW" if candidate.coverage_status != "READY"
            else "HIGH" if candidate.opportunity_score >= 75 or candidate.opportunity_score <= 35
            else "MEDIUM"
        )
        if action == "HOLD" and suppression_reasons:
            rationale = (
                f"Hold {candidate.ticker}: the proposed trade is suppressed by "
                f"{', '.join(suppression_reasons)}."
            )
        else:
            rationale = (
                f"{action.title()} {candidate.ticker}: score {candidate.opportunity_score} "
                f"and target weight {proposed_target}."
            )
        if tax_flags:
            rationale += " Review trading activity with a Swiss tax professional; this is not tax advice."

        signed_amount = signed_amount.quantize(_CENT, rounding=ROUND_HALF_UP)
        expected_edge_net = (expected_edge - costs.total_base).quantize(
            _CENT, rounding=ROUND_HALF_UP,
        ) if action != "HOLD" else Decimal("0")
        identity_payload = {
            "security_sk": candidate.security_sk,
            "action": action,
            "current_weight": str(candidate.current_weight),
            "target_weight": str(proposed_target),
            "suggested_amount_base": str(signed_amount),
            "as_of": as_of,
            "model_version": MODEL_VERSION,
        }
        recommendations.append(Recommendation(
            recommendation_id=_recommendation_id(identity_payload),
            security_sk=candidate.security_sk,
            ticker=candidate.ticker,
            action=action,
            current_weight=candidate.current_weight,
            target_weight=proposed_target,
            suggested_amount_base=signed_amount,
            estimated_cost_base=costs.total_base,
            expected_edge_base=expected_edge_net,
            confidence=confidence,
            rationale=rationale,
            suppression_reasons=tuple(suppression_reasons),
            tax_flags=tuple(tax_flags),
            as_of=as_of,
        ))
    return recommendations


def recommendation_payload(recommendation: Recommendation) -> dict:
    payload = asdict(recommendation)
    for field in (
        "current_weight", "target_weight", "suggested_amount_base",
        "estimated_cost_base", "expected_edge_base",
    ):
        payload[field] = str(payload[field])
    payload["suppression_reasons"] = list(recommendation.suppression_reasons)
    payload["tax_flags"] = list(recommendation.tax_flags)
    return payload