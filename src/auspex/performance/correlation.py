"""Composite/leg IC and leg correlation matrix (arc42 §5.8).

- **Composite IC**: Spearman rank correlation between composite percentile
  and forward 21/63/126-day return, computed cross-sectionally per date then
  averaged.
- **Leg IC**: same, per leg.
- **Leg correlation matrix**: pairwise Pearson across the six legs — reveals
  when six legs are really three.

Returns use USD, never CHF (arc42 §5.8): "CHF returns blend security
selection with a currency move, and only the first is evidence about the
model."
"""

from __future__ import annotations

from decimal import Decimal

from auspex.models.enums import LegName
from auspex.performance.ic import pearson, spearman_ic


def composite_ic_for_date(percentiles: list[Decimal], forward_returns_usd: list[Decimal]) -> Decimal | None:
    return spearman_ic(percentiles, forward_returns_usd)


def average_ic(per_date_ics: list[Decimal | None]) -> Decimal | None:
    valid = [ic for ic in per_date_ics if ic is not None]
    if not valid:
        return None
    return sum(valid, Decimal(0)) / Decimal(len(valid))


def leg_ic_for_date(leg_zs: list[Decimal], forward_returns_usd: list[Decimal]) -> Decimal | None:
    return spearman_ic(leg_zs, forward_returns_usd)


def leg_correlation_matrix(
    leg_values_by_leg: dict[LegName, list[Decimal]],
) -> dict[tuple[LegName, LegName], Decimal | None]:
    """Pairwise Pearson correlation across the six legs' cross-sectional z-scores."""

    legs = list(leg_values_by_leg)
    matrix: dict[tuple[LegName, LegName], Decimal | None] = {}
    for i, leg_a in enumerate(legs):
        for leg_b in legs[i:]:
            corr = pearson(leg_values_by_leg[leg_a], leg_values_by_leg[leg_b])
            matrix[(leg_a, leg_b)] = corr
            matrix[(leg_b, leg_a)] = corr
    return matrix
