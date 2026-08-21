from datetime import date, timedelta
from decimal import Decimal

from auspex.models.market import PriceBar
from auspex.policy.risk import (
    MarketRiskEstimate,
    correlation_groups,
    estimate_market_risk,
)


def bars(prices: list[str], *, volume: int = 1000) -> list[PriceBar]:
    start = date(2026, 1, 1)
    return [
        PriceBar(
            id=f"security:{start + timedelta(days=index)}",
            security_id="security",
            session_date=start + timedelta(days=index),
            open_raw=price,
            high_raw=price,
            low_raw=price,
            close_raw=price,
            close_adjusted=price,
            volume=volume,
        )
        for index, price in enumerate(prices)
    ]


def test_estimates_volatility_and_average_daily_value():
    estimate = estimate_market_risk(
        bars(["100", "101", "100", "102"]),
        fx_rate_to_chf=Decimal("0.8"),
        sessions=3,
    )

    assert estimate.volatility_60d is not None
    assert estimate.volatility_60d > 0
    assert estimate.average_daily_value_chf == Decimal("80800")
    assert len(estimate.returns) == 3


def test_groups_only_highly_correlated_series():
    start = date(2026, 1, 1)
    common = tuple(
        (
            start + timedelta(days=index),
            Decimal(index) / Decimal(100),
        )
        for index in range(1, 31)
    )
    inverse = tuple((session_date, -value) for session_date, value in common)
    estimates = {
        "a": MarketRiskEstimate(Decimal("0.2"), None, common),
        "b": MarketRiskEstimate(Decimal("0.2"), None, common),
        "c": MarketRiskEstimate(Decimal("0.2"), None, inverse),
    }

    groups = correlation_groups(estimates)

    assert groups["a"] == groups["b"]
    assert "c" not in groups


def test_correlation_joins_on_session_date_not_vector_position():
    start = date(2026, 1, 1)
    left = tuple(
        (start + timedelta(days=index), Decimal(index))
        for index in range(30)
    )
    right = tuple(
        (start + timedelta(days=index), Decimal(index))
        for index in range(5, 35)
    )
    estimates = {
        "a": MarketRiskEstimate(Decimal("0.2"), None, left),
        "b": MarketRiskEstimate(Decimal("0.2"), None, right),
    }

    groups = correlation_groups(
        estimates,
        threshold=Decimal("0.99"),
        min_observations=20,
    )

    assert groups["a"] == groups["b"]
