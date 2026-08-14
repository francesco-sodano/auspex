"""Composite score, percentile, and direction (arc42 §5.5 "Composite").

```
composite  = sum(weight_i * winsorise(z_i, 2.5)) / sum(weight_i for computable legs)
percentile = percentile_rank(composite, within=scope)
direction  = STRENGTHENING if delta7d(composite) > +0.15
             WEAKENING     if delta7d(composite) < -0.15
             else STABLE
```
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import Direction, LegName
from auspex.scoring.normalize import mean_std, percentile_rank, winsorise, zscore

DIRECTION_UP_THRESHOLD = Decimal("0.15")
DIRECTION_DOWN_THRESHOLD = Decimal("-0.15")


@dataclass(frozen=True)
class LegCompositeResult:
    raw: Decimal | None
    z: Decimal | None
    weight: Decimal
    contribution: Decimal | None
    computable: bool


@dataclass(frozen=True)
class CompositeResult:
    legs: dict[LegName, LegCompositeResult]
    composite: Decimal | None
    weight_sum: Decimal


def cross_sectional_zscores(cohort_raw_map: dict[str, Decimal | None]) -> dict[str, Decimal | None]:
    """z-score every non-None raw value against the others in ``cohort_raw_map``."""

    values = [v for v in cohort_raw_map.values() if v is not None]
    mean, std = mean_std(values)
    result: dict[str, Decimal | None] = {}
    for sid, raw in cohort_raw_map.items():
        if raw is None or mean is None or std is None:
            result[sid] = None
            continue
        result[sid] = zscore(raw, mean, std)
    return result


def compute_security_composite(
    leg_raw_by_leg: dict[LegName, Decimal | None],
    cohort_raw_by_leg: dict[LegName, dict[str, Decimal | None]],
    weights: dict[LegName, Decimal],
    security_id: str,
    winsor_sigma: Decimal = Decimal("2.5"),
) -> CompositeResult:
    """Compute one security's composite given the full cohort's raw leg values.

    ``cohort_raw_by_leg[leg]`` must include ``security_id`` itself alongside
    its cohort peers so the z-score is computed against the correct
    cross-section.
    """

    leg_results: dict[LegName, LegCompositeResult] = {}
    weighted_sum = Decimal(0)
    weight_sum = Decimal(0)

    for leg, weight in weights.items():
        raw = leg_raw_by_leg.get(leg)
        cohort_map = cohort_raw_by_leg.get(leg, {})
        z_map = cross_sectional_zscores(cohort_map)
        z = z_map.get(security_id)

        if raw is None or z is None:
            leg_results[leg] = LegCompositeResult(raw=raw, z=None, weight=weight, contribution=None, computable=False)
            continue

        z_w = winsorise(z, winsor_sigma)
        contribution = weight * z_w
        leg_results[leg] = LegCompositeResult(raw=raw, z=z_w, weight=weight, contribution=contribution, computable=True)
        weighted_sum += contribution
        weight_sum += weight

    composite = weighted_sum / weight_sum if weight_sum > 0 else None
    return CompositeResult(legs=leg_results, composite=composite, weight_sum=weight_sum)


def classify_direction(delta_7d: Decimal | None) -> Direction:
    if delta_7d is None:
        return Direction.STABLE
    if delta_7d > DIRECTION_UP_THRESHOLD:
        return Direction.STRENGTHENING
    if delta_7d < DIRECTION_DOWN_THRESHOLD:
        return Direction.WEAKENING
    return Direction.STABLE


def compute_percentile(security_id: str, composites: dict[str, Decimal | None]) -> int | None:
    own = composites.get(security_id)
    if own is None:
        return None
    population = [v for v in composites.values() if v is not None]
    return percentile_rank(own, population)
