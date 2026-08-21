"""IC distribution, ICIR and overlap-aware significance (arc42 §5.8).

A single averaged IC hides whether the signal was steady or one lucky quarter.
This module publishes the *shape* of the per-date IC series — dispersion,
quantiles, fraction of dates positive — plus the information ratio (ICIR) and a
t-statistic that uses the effective non-overlapping sample size rather than the
raw date count, because daily observations of a 21/63/126-session forward
return are heavily overlapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.performance.stats import (
    ZERO,
    effective_sample_size,
    mean,
    median,
    quantile,
    sample_std,
    two_sided_p_value,
)


@dataclass(frozen=True)
class ICDistribution:
    horizon_days: int
    count: int
    mean: Decimal
    std: Decimal | None
    minimum: Decimal
    q10: Decimal
    q25: Decimal
    median: Decimal
    q75: Decimal
    q90: Decimal
    maximum: Decimal
    positive_fraction: Decimal
    icir: Decimal | None
    effective_sample_size: Decimal
    t_statistic: Decimal | None
    p_value: Decimal | None


def information_ratio(series: list[Decimal]) -> Decimal | None:
    """ICIR — mean IC divided by its standard deviation across dates."""

    avg = mean(series)
    std = sample_std(series)
    if avg is None or std is None or std == 0:
        return None
    return avg / std


def _quantile(series: list[Decimal], q: str) -> Decimal:
    value = quantile(series, Decimal(q))
    return ZERO if value is None else value


def _median(series: list[Decimal]) -> Decimal:
    value = median(series)
    return ZERO if value is None else value


def ic_distribution(per_date_ics: list[Decimal | None], horizon_days: int) -> ICDistribution | None:
    """Distributional summary of a per-date IC series, ``None`` when empty."""

    series = [ic for ic in per_date_ics if ic is not None]
    if not series:
        return None

    avg = sum(series, ZERO) / Decimal(len(series))
    std = sample_std(series)
    ess = effective_sample_size(len(series), horizon_days)
    positives = sum(1 for ic in series if ic > 0)

    t_statistic: Decimal | None = None
    p_value: Decimal | None = None
    if std is not None and std > 0 and ess > 0:
        t_statistic = avg / (std / ess.sqrt())
        if ess >= Decimal("10"):
            p_value = two_sided_p_value(t_statistic)

    return ICDistribution(
        horizon_days=horizon_days,
        count=len(series),
        mean=avg,
        std=std,
        minimum=min(series),
        q10=_quantile(series, "0.10"),
        q25=_quantile(series, "0.25"),
        median=_median(series),
        q75=_quantile(series, "0.75"),
        q90=_quantile(series, "0.90"),
        maximum=max(series),
        positive_fraction=Decimal(positives) / Decimal(len(series)),
        icir=information_ratio(series),
        effective_sample_size=ess,
        t_statistic=t_statistic,
        p_value=p_value,
    )
