"""Deterministic market-risk estimates used by joint allocation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.models.market import PriceBar
from auspex.scoring.normalize import mean_std

TRADING_SESSIONS_PER_YEAR = Decimal(252)


@dataclass(frozen=True)
class MarketRiskEstimate:
    volatility_60d: Decimal | None
    average_daily_value_chf: Decimal | None
    returns: tuple[tuple[date, Decimal], ...]


def estimate_market_risk(
    bars: list[PriceBar],
    *,
    fx_rate_to_chf: Decimal,
    sessions: int = 60,
) -> MarketRiskEstimate:
    """Annualized close-to-close volatility and average daily value."""

    ordered = sorted(bars, key=lambda item: item.session_date)[
        -(max(sessions, 2) + 1) :
    ]
    returns = tuple(
        (
            current.session_date,
            Decimal(current.close_adjusted)
            / Decimal(previous.close_adjusted)
            - Decimal(1),
        )
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if Decimal(previous.close_adjusted) > 0
        and Decimal(current.close_adjusted) > 0
    )
    _, daily_std = mean_std([value for _, value in returns])
    annualized = (
        daily_std * TRADING_SESSIONS_PER_YEAR.sqrt()
        if daily_std is not None
        else None
    )
    values = [
        Decimal(bar.close_adjusted)
        * Decimal(bar.volume)
        * fx_rate_to_chf
        for bar in ordered[-sessions:]
        if Decimal(bar.close_adjusted) > 0 and bar.volume > 0
    ]
    average_daily_value = (
        sum(values, Decimal(0)) / Decimal(len(values))
        if values
        else None
    )
    return MarketRiskEstimate(
        volatility_60d=annualized,
        average_daily_value_chf=average_daily_value,
        returns=returns,
    )


def correlation_groups(
    estimates: dict[str, MarketRiskEstimate],
    *,
    threshold: Decimal = Decimal("0.85"),
    min_observations: int = 20,
) -> dict[str, str]:
    """Connected components of securities with highly correlated returns."""

    parents = {security_id: security_id for security_id in estimates}

    def find(item: str) -> str:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        smaller, larger = sorted((left_root, right_root))
        parents[larger] = smaller

    security_ids = sorted(estimates)
    for index, left in enumerate(security_ids):
        for right in security_ids[index + 1 :]:
            value = _correlation(
                estimates[left].returns,
                estimates[right].returns,
                min_observations=min_observations,
            )
            if value is not None and value >= threshold:
                union(left, right)

    members: dict[str, list[str]] = {}
    for security_id in security_ids:
        members.setdefault(find(security_id), []).append(security_id)
    result: dict[str, str] = {}
    for component in members.values():
        if len(component) < 2:
            continue
        group_id = f"corr:{component[0]}"
        for security_id in component:
            result[security_id] = group_id
    return result


def _correlation(
    left: tuple[tuple[date, Decimal], ...],
    right: tuple[tuple[date, Decimal], ...],
    *,
    min_observations: int,
) -> Decimal | None:
    left_by_date = dict(left)
    right_by_date = dict(right)
    shared_dates = sorted(set(left_by_date) & set(right_by_date))
    count = len(shared_dates)
    if count < min_observations:
        return None
    left_values = [left_by_date[session_date] for session_date in shared_dates]
    right_values = [
        right_by_date[session_date] for session_date in shared_dates
    ]
    left_mean, left_std = mean_std(left_values)
    right_mean, right_std = mean_std(right_values)
    if (
        left_mean is None
        or right_mean is None
        or left_std is None
        or right_std is None
        or left_std == 0
        or right_std == 0
    ):
        return None
    covariance = sum(
        (
            (left_value - left_mean)
            * (right_value - right_mean)
            for left_value, right_value in zip(
                left_values,
                right_values,
                strict=True,
            )
        ),
        Decimal(0),
    ) / Decimal(count)
    return covariance / (left_std * right_std)
