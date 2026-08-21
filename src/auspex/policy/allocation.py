"""Deterministic joint allocation of portfolio-policy candidates.

The score engine decides which trades are worth considering. This module
decides which of those candidates can coexist inside one portfolio. It never
changes a score or invents an action: it only reduces BUY/ADD notionals so the
published set is executable under shared cash, concentration, volatility,
liquidity, and turnover constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from auspex.models.enums import Action, Direction
from auspex.models.policy import GateResult
from auspex.models.user_settings import InvestmentHorizon, InvestmentObjective

ZERO = Decimal(0)
ONE = Decimal(1)
MIN_VOLATILITY_SCALE = Decimal("0.50")
MAX_VOLATILITY_SCALE = Decimal("1.25")


@dataclass(frozen=True)
class AllocationCandidate:
    security_id: str
    ticker: str
    cohort: str
    correlation_group: str | None
    action: Action
    percentile: int
    direction: Direction
    requested_trade_chf: Decimal
    current_weight_pct: Decimal
    estimated_cost_chf: Decimal = ZERO
    volatility_60d: Decimal | None = None
    average_daily_value_chf: Decimal | None = None


@dataclass(frozen=True)
class AllocationConstraints:
    cash_chf: Decimal
    cash_reserve_chf: Decimal
    total_value_chf: Decimal
    max_position_pct: Decimal
    max_cohort_pct: Decimal
    max_correlated_group_pct: Decimal
    max_buy_turnover_pct: Decimal
    max_daily_volume_participation: Decimal
    min_trade_chf: Decimal
    enforce_liquidity: bool = False
    target_volatility_60d: Decimal | None = None
    current_cohort_weights_pct: dict[str, Decimal] = field(default_factory=dict)
    current_correlated_group_weights_pct: dict[str, Decimal] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class AllocationDecision:
    security_id: str
    allocated_trade_chf: Decimal
    requested_trade_chf: Decimal
    volatility_scale: Decimal
    cash_limited: bool
    position_limited: bool
    cohort_limited: bool
    correlation_limited: bool
    liquidity_limited: bool
    turnover_limited: bool
    below_minimum_trade: bool


_OBJECTIVE_COHORT_CAP = {
    InvestmentObjective.CAPITAL_PRESERVATION: Decimal("20"),
    InvestmentObjective.INCOME: Decimal("25"),
    InvestmentObjective.BALANCED_GROWTH: Decimal("30"),
    InvestmentObjective.CAPITAL_GROWTH: Decimal("35"),
}

_OBJECTIVE_TURNOVER_CAP = {
    InvestmentObjective.CAPITAL_PRESERVATION: Decimal("4"),
    InvestmentObjective.INCOME: Decimal("5"),
    InvestmentObjective.BALANCED_GROWTH: Decimal("7.5"),
    InvestmentObjective.CAPITAL_GROWTH: Decimal("10"),
}

_HORIZON_TURNOVER_MULTIPLIER = {
    InvestmentHorizon.SIX_MONTHS: Decimal("1.00"),
    InvestmentHorizon.ONE_YEAR: Decimal("0.90"),
    InvestmentHorizon.ONE_TO_THREE_YEARS: Decimal("0.75"),
    InvestmentHorizon.THREE_TO_SEVEN_YEARS: Decimal("0.60"),
    InvestmentHorizon.OVER_SEVEN_YEARS: Decimal("0.50"),
}


def preference_constraints(
    *,
    horizon: InvestmentHorizon,
    objective: InvestmentObjective,
    policy_max_position_pct: Decimal,
    cash_chf: Decimal,
    cash_reserve_chf: Decimal,
    total_value_chf: Decimal,
    min_trade_chf: Decimal,
    current_cohort_weights_pct: dict[str, Decimal],
    max_daily_volume_participation: Decimal = Decimal("0.01"),
    current_correlated_group_weights_pct: dict[str, Decimal] | None = None,
    allocation_config: dict | None = None,
) -> AllocationConstraints:
    """Translate user preferences into explicit, inspectable risk limits."""

    config = allocation_config or {}
    objective_config = config.get("objective_limits", {}).get(
        objective.value,
        {},
    )
    objective_position_cap = Decimal(
        str(
            objective_config.get(
                "max_position_pct",
                {
                    InvestmentObjective.CAPITAL_PRESERVATION: Decimal("8"),
                    InvestmentObjective.INCOME: Decimal("10"),
                    InvestmentObjective.BALANCED_GROWTH: Decimal("12.5"),
                    InvestmentObjective.CAPITAL_GROWTH: policy_max_position_pct,
                }[objective],
            )
        )
    )
    cohort_cap = Decimal(
        str(
            objective_config.get(
                "max_cohort_pct",
                _OBJECTIVE_COHORT_CAP[objective],
            )
        )
    )
    correlated_group_cap = Decimal(
        str(
            objective_config.get(
                "max_correlated_group_pct",
                min(cohort_cap, Decimal("25")),
            )
        )
    )
    objective_turnover = Decimal(
        str(
            objective_config.get(
                "max_buy_turnover_pct",
                _OBJECTIVE_TURNOVER_CAP[objective],
            )
        )
    )
    target_volatility = Decimal(
        str(
            objective_config.get(
                "target_volatility_60d",
                {
                    InvestmentObjective.CAPITAL_PRESERVATION: "0.15",
                    InvestmentObjective.INCOME: "0.20",
                    InvestmentObjective.BALANCED_GROWTH: "0.30",
                    InvestmentObjective.CAPITAL_GROWTH: "0.40",
                }[objective],
            )
        )
    )
    horizon_multiplier = Decimal(
        str(
            config.get("horizon_turnover_multiplier", {}).get(
                horizon.value,
                _HORIZON_TURNOVER_MULTIPLIER[horizon],
            )
        )
    )
    turnover = (
        objective_turnover
        * horizon_multiplier
    )
    return AllocationConstraints(
        cash_chf=max(cash_chf, ZERO),
        cash_reserve_chf=max(cash_reserve_chf, ZERO),
        total_value_chf=max(total_value_chf, ZERO),
        max_position_pct=min(policy_max_position_pct, objective_position_cap),
        max_cohort_pct=cohort_cap,
        max_correlated_group_pct=correlated_group_cap,
        max_buy_turnover_pct=turnover,
        max_daily_volume_participation=max(
            max_daily_volume_participation,
            ZERO,
        ),
        min_trade_chf=max(min_trade_chf, ZERO),
        enforce_liquidity=True,
        target_volatility_60d=target_volatility,
        current_cohort_weights_pct=current_cohort_weights_pct,
        current_correlated_group_weights_pct=(
            current_correlated_group_weights_pct or {}
        ),
    )


def allocate_candidates(
    candidates: list[AllocationCandidate],
    constraints: AllocationConstraints,
) -> dict[str, AllocationDecision]:
    """Allocate all candidates against one shared portfolio budget.

    SELL/TRIM rows are preserved because they release risk and cash. BUY/ADD
    rows are processed by deterministic priority: percentile, strengthening
    direction, BUY before ADD, then stable security id.
    """

    decisions: dict[str, AllocationDecision] = {}
    buy_candidates = [
        candidate
        for candidate in candidates
        if candidate.action in {Action.BUY, Action.ADD}
    ]
    for candidate in candidates:
        if candidate.action not in {Action.BUY, Action.ADD}:
            decisions[candidate.security_id] = _unchanged_decision(candidate)

    remaining_cash = max(
        constraints.cash_chf - constraints.cash_reserve_chf,
        ZERO,
    )
    remaining_cash += sum(
        max(candidate.requested_trade_chf - candidate.estimated_cost_chf, ZERO)
        for candidate in candidates
        if candidate.action in {Action.TRIM, Action.SELL}
    )
    turnover_budget = (
        constraints.total_value_chf
        * constraints.max_buy_turnover_pct
        / Decimal(100)
    )
    remaining_turnover = max(turnover_budget, ZERO)
    cohort_weights = dict(constraints.current_cohort_weights_pct)
    correlated_group_weights = dict(
        constraints.current_correlated_group_weights_pct
    )
    reference_volatility = _median(
        [
            candidate.volatility_60d
            for candidate in buy_candidates
            if candidate.volatility_60d is not None
            and candidate.volatility_60d > ZERO
        ]
    )

    ordered = sorted(
        buy_candidates,
        key=lambda candidate: (
            -candidate.percentile,
            0 if candidate.direction is Direction.STRENGTHENING else 1,
            0 if candidate.action is Action.BUY else 1,
            candidate.security_id,
        ),
    )
    for candidate in ordered:
        volatility_scale = _volatility_scale(
            candidate.volatility_60d,
            reference_volatility,
            constraints.target_volatility_60d,
        )
        requested = max(candidate.requested_trade_chf, ZERO)
        risk_adjusted = requested * volatility_scale
        position_capacity = _weight_capacity_chf(
            constraints.total_value_chf,
            constraints.max_position_pct - candidate.current_weight_pct,
        )
        cohort_capacity = _weight_capacity_chf(
            constraints.total_value_chf,
            constraints.max_cohort_pct
            - cohort_weights.get(candidate.cohort, ZERO),
        )
        correlated_group_capacity = (
            _weight_capacity_chf(
                constraints.total_value_chf,
                constraints.max_correlated_group_pct
                - correlated_group_weights.get(
                    candidate.correlation_group,
                    ZERO,
                ),
            )
            if candidate.correlation_group is not None
            else risk_adjusted
        )
        if (
            candidate.average_daily_value_chf is not None
            and candidate.average_daily_value_chf > ZERO
        ):
            liquidity_capacity = (
                candidate.average_daily_value_chf
                * constraints.max_daily_volume_participation
            )
        else:
            liquidity_capacity = (
                ZERO if constraints.enforce_liquidity else risk_adjusted
            )
        cash_capacity = max(
            remaining_cash - candidate.estimated_cost_chf,
            ZERO,
        )
        raw_allocated = min(
            risk_adjusted,
            cash_capacity,
            remaining_turnover,
            position_capacity,
            cohort_capacity,
            correlated_group_capacity,
            liquidity_capacity,
        )
        below_minimum = (
            ZERO < raw_allocated < constraints.min_trade_chf
        )
        allocated = ZERO if below_minimum else raw_allocated
        scaled_cost = (
            candidate.estimated_cost_chf
            * allocated
            / requested
            if requested > ZERO and allocated > ZERO
            else ZERO
        )
        cash_limited = raw_allocated < risk_adjusted and cash_capacity <= min(
            risk_adjusted,
            remaining_turnover,
            position_capacity,
            cohort_capacity,
            correlated_group_capacity,
            liquidity_capacity,
        )
        position_limited = (
            raw_allocated < risk_adjusted
            and position_capacity
            <= min(
                risk_adjusted,
                cash_capacity,
                remaining_turnover,
                cohort_capacity,
                correlated_group_capacity,
                liquidity_capacity,
            )
        )
        cohort_limited = (
            raw_allocated < risk_adjusted
            and cohort_capacity
            <= min(
                risk_adjusted,
                cash_capacity,
                remaining_turnover,
                position_capacity,
                correlated_group_capacity,
                liquidity_capacity,
            )
        )
        correlation_limited = (
            raw_allocated < risk_adjusted
            and correlated_group_capacity
            <= min(
                risk_adjusted,
                cash_capacity,
                remaining_turnover,
                position_capacity,
                cohort_capacity,
                liquidity_capacity,
            )
        )
        liquidity_limited = (
            raw_allocated < risk_adjusted
            and liquidity_capacity
            <= min(
                risk_adjusted,
                cash_capacity,
                remaining_turnover,
                position_capacity,
                cohort_capacity,
                correlated_group_capacity,
            )
        )
        turnover_limited = (
            raw_allocated < risk_adjusted
            and remaining_turnover
            <= min(
                risk_adjusted,
                cash_capacity,
                position_capacity,
                cohort_capacity,
                correlated_group_capacity,
                liquidity_capacity,
            )
        )
        if below_minimum:
            cash_limited = False
            position_limited = False
            cohort_limited = False
            correlation_limited = False
            liquidity_limited = False
            turnover_limited = False

        decision = AllocationDecision(
            security_id=candidate.security_id,
            allocated_trade_chf=allocated,
            requested_trade_chf=requested,
            volatility_scale=volatility_scale,
            cash_limited=cash_limited,
            position_limited=position_limited,
            cohort_limited=cohort_limited,
            correlation_limited=correlation_limited,
            liquidity_limited=liquidity_limited,
            turnover_limited=turnover_limited,
            below_minimum_trade=below_minimum,
        )
        decisions[candidate.security_id] = decision
        remaining_cash -= allocated
        remaining_cash -= scaled_cost
        remaining_turnover -= allocated
        if constraints.total_value_chf > ZERO:
            cohort_weights[candidate.cohort] = (
                cohort_weights.get(candidate.cohort, ZERO)
                + allocated / constraints.total_value_chf * Decimal(100)
            )
            if candidate.correlation_group is not None:
                correlated_group_weights[candidate.correlation_group] = (
                    correlated_group_weights.get(
                        candidate.correlation_group,
                        ZERO,
                    )
                    + allocated
                    / constraints.total_value_chf
                    * Decimal(100)
                )

    return decisions


def allocation_gate_trace(
    decision: AllocationDecision,
) -> list[GateResult]:
    """Auditable reasons explaining how a requested trade was allocated."""

    requested = str(decision.requested_trade_chf)
    allocated = str(decision.allocated_trade_chf)
    return [
        GateResult(
            gate="joint_cash_budget",
            passed=not decision.cash_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.cash_limited
                else "shared CHF cash budget reduced this candidate"
            ),
        ),
        GateResult(
            gate="position_risk_limit",
            passed=not decision.position_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.position_limited
                else "maximum position exposure reduced this candidate"
            ),
        ),
        GateResult(
            gate="cohort_risk_limit",
            passed=not decision.cohort_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.cohort_limited
                else "maximum cohort exposure reduced this candidate"
            ),
        ),
        GateResult(
            gate="correlation_risk_limit",
            passed=not decision.correlation_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.correlation_limited
                else "highly correlated exposure reduced this candidate"
            ),
        ),
        GateResult(
            gate="liquidity_participation_limit",
            passed=not decision.liquidity_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.liquidity_limited
                else "daily-value participation reduced this candidate"
            ),
        ),
        GateResult(
            gate="turnover_budget",
            passed=not decision.turnover_limited,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.turnover_limited
                else "portfolio turnover budget reduced this candidate"
            ),
        ),
        GateResult(
            gate="allocated_trade_min",
            passed=not decision.below_minimum_trade,
            actual_value=allocated,
            threshold_value=requested,
            detail=(
                None
                if not decision.below_minimum_trade
                else "remaining allocation was below the executable minimum"
            ),
        ),
        GateResult(
            gate="volatility_scale",
            passed=True,
            actual_value=str(decision.volatility_scale),
            detail="relative 60-session volatility scaling",
        ),
    ]


def _unchanged_decision(
    candidate: AllocationCandidate,
) -> AllocationDecision:
    requested = max(candidate.requested_trade_chf, ZERO)
    return AllocationDecision(
        security_id=candidate.security_id,
        allocated_trade_chf=requested,
        requested_trade_chf=requested,
        volatility_scale=ONE,
        cash_limited=False,
        position_limited=False,
        cohort_limited=False,
        correlation_limited=False,
        liquidity_limited=False,
        turnover_limited=False,
        below_minimum_trade=False,
    )


def _weight_capacity_chf(
    total_value_chf: Decimal,
    remaining_weight_pct: Decimal,
) -> Decimal:
    if total_value_chf <= ZERO or remaining_weight_pct <= ZERO:
        return ZERO
    return total_value_chf * remaining_weight_pct / Decimal(100)


def _volatility_scale(
    volatility: Decimal | None,
    reference: Decimal | None,
    target: Decimal | None = None,
) -> Decimal:
    if volatility is None or volatility <= ZERO:
        return ONE
    relative = (
        reference / volatility
        if reference is not None and reference > ZERO
        else ONE
    )
    absolute = (
        target / volatility
        if target is not None and target > ZERO
        else MAX_VOLATILITY_SCALE
    )
    return min(
        max(min(relative, absolute), MIN_VOLATILITY_SCALE),
        MAX_VOLATILITY_SCALE,
    )


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)
