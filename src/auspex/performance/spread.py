"""Top-minus-bottom spread, turnover and cost-adjusted outcomes (arc42 §5.8).

IC measures monotonic ordering; it does not say whether the ordering is
*tradable*. The spread does: rank the cross-section, buy the top quantile, sell
the bottom, and report the difference — alongside the numbers that decide
whether the difference survives contact with reality:

- a **robust** spread computed from quantile-trimmed means, plus the count of
  observations that trimming removed, so a single squeeze cannot carry the
  result;
- **turnover** between consecutive selections, the driver of cost;
- **cost-adjusted** return, computed only when the caller supplies a cost per
  unit of turnover (the fee schedule lives in ``config/fees.yaml`` and is a
  portfolio-policy concern, so it is injected rather than assumed here);
- **max drawdown** of the compounded spread series.

Everything degrades to ``None`` when its inputs are absent rather than
substituting a placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.performance.stats import ONE, ZERO, mean, sample_std

DEFAULT_QUANTILE = Decimal("0.2")
DEFAULT_OUTLIER_SIGMA = Decimal("3")


@dataclass(frozen=True)
class SpreadResult:
    population: int
    top_count: int
    bottom_count: int
    top_return: Decimal
    bottom_return: Decimal
    spread: Decimal
    robust_spread: Decimal | None
    outlier_count: int
    top_ids: tuple[str, ...]
    bottom_ids: tuple[str, ...]


def _trimmed_mean(values: list[Decimal], outlier_sigma: Decimal) -> tuple[Decimal | None, int]:
    """Mean after dropping observations beyond ``outlier_sigma`` sample deviations."""

    if not values:
        return None, 0
    avg = mean(values)
    std = sample_std(values)
    if avg is None or std is None or std == 0:
        return avg, 0
    limit = outlier_sigma * std
    kept = [value for value in values if abs(value - avg) <= limit]
    if not kept:
        return avg, 0
    return sum(kept, ZERO) / Decimal(len(kept)), len(values) - len(kept)


def top_minus_bottom(
    scores_by_security: dict[str, Decimal],
    forward_returns_by_security: dict[str, Decimal],
    *,
    quantile_fraction: Decimal = DEFAULT_QUANTILE,
    outlier_sigma: Decimal = DEFAULT_OUTLIER_SIGMA,
) -> SpreadResult | None:
    """Return of the top quantile minus the bottom quantile, ranked by score."""

    shared = sorted(set(scores_by_security) & set(forward_returns_by_security))
    if len(shared) < 2:
        return None

    ranked = sorted(shared, key=lambda sid: (scores_by_security[sid], sid))
    bucket = int((Decimal(len(ranked)) * quantile_fraction).to_integral_value(rounding="ROUND_FLOOR"))
    bucket = max(1, min(bucket, len(ranked) // 2))

    bottom_ids = tuple(ranked[:bucket])
    top_ids = tuple(ranked[-bucket:])
    top_values = [forward_returns_by_security[sid] for sid in top_ids]
    bottom_values = [forward_returns_by_security[sid] for sid in bottom_ids]

    top_return = sum(top_values, ZERO) / Decimal(len(top_values))
    bottom_return = sum(bottom_values, ZERO) / Decimal(len(bottom_values))

    robust_top, top_outliers = _trimmed_mean(top_values, outlier_sigma)
    robust_bottom, bottom_outliers = _trimmed_mean(bottom_values, outlier_sigma)
    robust_spread = None if robust_top is None or robust_bottom is None else robust_top - robust_bottom

    return SpreadResult(
        population=len(shared),
        top_count=len(top_ids),
        bottom_count=len(bottom_ids),
        top_return=top_return,
        bottom_return=bottom_return,
        spread=top_return - bottom_return,
        robust_spread=robust_spread,
        outlier_count=top_outliers + bottom_outliers,
        top_ids=top_ids,
        bottom_ids=bottom_ids,
    )


def turnover(previous_ids: tuple[str, ...] | None, current_ids: tuple[str, ...]) -> Decimal | None:
    """One-sided name turnover between consecutive selections.

    ``None`` on the first date, when there is no prior selection to compare to.
    """

    if previous_ids is None:
        return None
    if not current_ids:
        return ZERO
    previous = set(previous_ids)
    changed = sum(1 for sid in current_ids if sid not in previous)
    return Decimal(changed) / Decimal(len(current_ids))


def cost_adjusted_return(
    gross_return: Decimal,
    turnover_fraction: Decimal | None,
    cost_per_unit_turnover: Decimal | None,
) -> Decimal | None:
    """Gross return net of trading friction, ``None`` when no cost input is supplied."""

    if turnover_fraction is None or cost_per_unit_turnover is None:
        return None
    return gross_return - turnover_fraction * cost_per_unit_turnover


def max_drawdown(period_returns: list[Decimal]) -> Decimal | None:
    """Largest peak-to-trough decline of the compounded series, as a positive fraction."""

    if len(period_returns) < 2:
        return None
    equity = ONE
    peak = ONE
    worst = ZERO
    for period_return in period_returns:
        equity *= ONE + period_return
        peak = max(peak, equity)
        if peak > 0:
            drawdown = (peak - equity) / peak
            worst = max(worst, drawdown)
    return worst
