from decimal import Decimal

from auspex.models.enums import Action, Direction
from auspex.models.user_settings import InvestmentHorizon, InvestmentObjective
from auspex.policy.allocation import (
    AllocationCandidate,
    AllocationConstraints,
    allocate_candidates,
    allocation_gate_trace,
    preference_constraints,
)


def candidate(
    security_id: str,
    *,
    percentile: int,
    requested: str = "5000",
    cohort: str = "compute",
    current_weight: str = "0",
    volatility: str | None = None,
    average_daily_value: str | None = None,
    action: Action = Action.BUY,
    correlation_group: str | None = None,
    estimated_cost: str = "0",
) -> AllocationCandidate:
    return AllocationCandidate(
        security_id=security_id,
        ticker=security_id.upper(),
        cohort=cohort,
        correlation_group=correlation_group,
        action=action,
        percentile=percentile,
        direction=Direction.STABLE,
        requested_trade_chf=Decimal(requested),
        current_weight_pct=Decimal(current_weight),
        estimated_cost_chf=Decimal(estimated_cost),
        volatility_60d=Decimal(volatility) if volatility else None,
        average_daily_value_chf=(
            Decimal(average_daily_value)
            if average_daily_value
            else None
        ),
    )


def constraints(**overrides) -> AllocationConstraints:
    values = {
        "cash_chf": Decimal("13000"),
        "cash_reserve_chf": Decimal("3000"),
        "total_value_chf": Decimal("100000"),
        "max_position_pct": Decimal("15"),
        "max_cohort_pct": Decimal("35"),
        "max_correlated_group_pct": Decimal("25"),
        "max_buy_turnover_pct": Decimal("10"),
        "max_daily_volume_participation": Decimal("0.01"),
        "min_trade_chf": Decimal("1000"),
        "enforce_liquidity": False,
        "target_volatility_60d": None,
        "current_cohort_weights_pct": {},
    }
    values.update(overrides)
    return AllocationConstraints(**values)


def test_shared_cash_is_allocated_once_in_priority_order():
    decisions = allocate_candidates(
        [
            candidate("low", percentile=75, requested="7000"),
            candidate("high", percentile=95, requested="7000"),
        ],
        constraints(),
    )

    assert decisions["high"].allocated_trade_chf == Decimal("7000")
    assert decisions["low"].allocated_trade_chf == Decimal("3000")
    assert sum(
        decision.allocated_trade_chf
        for decision in decisions.values()
    ) == Decimal("10000")


def test_below_minimum_remainder_is_not_published():
    decisions = allocate_candidates(
        [
            candidate("high", percentile=95, requested="9500"),
            candidate("low", percentile=75, requested="5000"),
        ],
        constraints(),
    )

    assert decisions["high"].allocated_trade_chf == Decimal("9500")
    assert decisions["low"].allocated_trade_chf == 0
    assert decisions["low"].below_minimum_trade is True


def test_position_and_cohort_caps_are_joint_constraints():
    decisions = allocate_candidates(
        [
            candidate(
                "position",
                percentile=95,
                requested="10000",
                current_weight="14",
            ),
            candidate(
                "cohort",
                percentile=90,
                requested="10000",
                cohort="power",
            ),
        ],
        constraints(
            current_cohort_weights_pct={
                "compute": Decimal("20"),
                "power": Decimal("34"),
            }
        ),
    )

    assert decisions["position"].allocated_trade_chf == Decimal("1000")
    assert decisions["position"].position_limited is True
    assert decisions["cohort"].allocated_trade_chf == Decimal("1000")
    assert decisions["cohort"].cohort_limited is True


def test_high_volatility_and_low_liquidity_reduce_allocation():
    decisions = allocate_candidates(
        [
            candidate("stable", percentile=90, volatility="0.20"),
            candidate(
                "volatile",
                percentile=85,
                volatility="0.80",
                average_daily_value="100000",
            ),
        ],
        constraints(),
    )

    assert decisions["stable"].volatility_scale == Decimal("1.25")
    assert decisions["volatile"].volatility_scale == Decimal("0.625")
    assert decisions["volatile"].allocated_trade_chf == Decimal("1000")
    assert decisions["volatile"].liquidity_limited is True


def test_sales_are_not_reduced_by_buy_budget():
    decisions = allocate_candidates(
        [
            candidate(
                "sell",
                percentile=10,
                requested="8000",
                action=Action.SELL,
            )
        ],
        constraints(cash_chf=Decimal("0")),
    )

    assert decisions["sell"].allocated_trade_chf == Decimal("8000")


def test_highly_correlated_candidates_share_an_exposure_cap():
    decisions = allocate_candidates(
        [
            candidate(
                "first",
                percentile=95,
                requested="8000",
                correlation_group="corr:ai",
            ),
            candidate(
                "second",
                percentile=90,
                requested="8000",
                correlation_group="corr:ai",
            ),
        ],
        constraints(
            cash_chf=Decimal("30000"),
            max_buy_turnover_pct=Decimal("30"),
            max_correlated_group_pct=Decimal("10"),
        ),
    )

    assert decisions["first"].allocated_trade_chf == Decimal("8000")
    assert decisions["second"].allocated_trade_chf == Decimal("2000")
    assert decisions["second"].correlation_limited is True


def test_preferences_produce_stricter_long_term_preservation_limits():
    result = preference_constraints(
        horizon=InvestmentHorizon.OVER_SEVEN_YEARS,
        objective=InvestmentObjective.CAPITAL_PRESERVATION,
        policy_max_position_pct=Decimal("15"),
        cash_chf=Decimal("10000"),
        cash_reserve_chf=Decimal("3000"),
        total_value_chf=Decimal("100000"),
        min_trade_chf=Decimal("2000"),
        current_cohort_weights_pct={},
    )

    assert result.max_position_pct == Decimal("8")
    assert result.max_cohort_pct == Decimal("20")
    assert result.max_buy_turnover_pct == Decimal("2.00")
    assert result.target_volatility_60d == Decimal("0.15")


def test_allocation_trace_explains_binding_constraints():
    decision = allocate_candidates(
        [
            candidate("high", percentile=95, requested="8000"),
            candidate("low", percentile=75, requested="5000"),
        ],
        constraints(),
    )["low"]

    trace = {gate.gate: gate for gate in allocation_gate_trace(decision)}

    assert trace["joint_cash_budget"].passed is False
    assert trace["allocated_trade_min"].passed is True
    assert trace["volatility_scale"].actual_value == "1"


def test_shared_cash_reserves_estimated_trade_costs():
    decisions = allocate_candidates(
        [
            candidate(
                "first",
                percentile=95,
                requested="5000",
                estimated_cost="100",
            ),
            candidate(
                "second",
                percentile=90,
                requested="5000",
                estimated_cost="100",
            ),
        ],
        constraints(
            cash_chf=Decimal("13000"),
            cash_reserve_chf=Decimal("3000"),
            min_trade_chf=Decimal("0"),
        ),
    )

    assert decisions["first"].allocated_trade_chf == Decimal("5000")
    assert decisions["second"].allocated_trade_chf == Decimal("4800")


def test_risk_allocation_fails_closed_without_liquidity_data():
    decision = allocate_candidates(
        [candidate("unknown", percentile=95)],
        constraints(enforce_liquidity=True),
    )["unknown"]

    assert decision.allocated_trade_chf == 0
    assert decision.liquidity_limited is True


def test_minimum_trade_reason_is_not_misattributed():
    decision = allocate_candidates(
        [candidate("small", percentile=95, requested="500")],
        constraints(),
    )["small"]

    assert decision.below_minimum_trade is True
    assert decision.cash_limited is False
    assert decision.position_limited is False
    assert decision.cohort_limited is False
    assert decision.correlation_limited is False
    assert decision.liquidity_limited is False
    assert decision.turnover_limited is False


def test_sales_can_fund_joint_buy_allocation():
    decisions = allocate_candidates(
        [
            candidate(
                "sell",
                percentile=10,
                requested="5000",
                action=Action.SELL,
            ),
            candidate("buy", percentile=95, requested="5000"),
        ],
        constraints(
            cash_chf=Decimal("3000"),
            cash_reserve_chf=Decimal("3000"),
        ),
    )

    assert decisions["buy"].allocated_trade_chf == Decimal("5000")


def test_preference_limits_are_versioned_configuration():
    result = preference_constraints(
        horizon=InvestmentHorizon.ONE_YEAR,
        objective=InvestmentObjective.CAPITAL_GROWTH,
        policy_max_position_pct=Decimal("20"),
        cash_chf=Decimal("10000"),
        cash_reserve_chf=Decimal("3000"),
        total_value_chf=Decimal("100000"),
        min_trade_chf=Decimal("2000"),
        current_cohort_weights_pct={},
        allocation_config={
            "objective_limits": {
                "CAPITAL_GROWTH": {
                    "max_position_pct": "14",
                    "max_cohort_pct": "28",
                    "max_correlated_group_pct": "21",
                    "max_buy_turnover_pct": "8",
                }
            },
            "horizon_turnover_multiplier": {"ONE_YEAR": "0.5"},
        },
    )

    assert result.max_position_pct == Decimal("14")
    assert result.max_cohort_pct == Decimal("28")
    assert result.max_correlated_group_pct == Decimal("21")
    assert result.max_buy_turnover_pct == Decimal("4.0")
