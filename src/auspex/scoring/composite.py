"""Composite score, percentile, and direction (arc42 §5.5 "Composite").

```
composite  = sum(weight_i * winsorise(z_i, 2.5)) / sum(weight_i for applicable legs)
percentile = percentile_rank(composite, within=scope)
direction  = STRENGTHENING if delta7d(composite) > +0.15
             WEAKENING     if delta7d(composite) < -0.15
             else STABLE
```

Legs are three-state rather than two-state:

* **applicable and computable** — the winsorised z contributes ``weight * z``;
* **applicable but not computable** — the leg contributes a *neutral* ``z = 0``
  while keeping its full weight in the denominator. A security is not rewarded
  for a leg it cannot evidence, and the composite scale stays comparable across
  securities with different data availability;
* **not applicable** — a structural exclusion (SMART_MONEY for an FPI; the
  valuation brake when point-in-time FX for a non-USD reporter is unavailable).
  These legs leave both the numerator and the denominator entirely, and are
  likewise removed from the coverage denominator, so an issuer is never
  penalised for a leg that cannot exist for it.

Coverage and confidence remain reported separately (``coverage()`` in
``auspex.scoring.coverage`` and ``CohortScope.confidence``); the composite no
longer silently encodes missingness by renormalising weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import Direction, LegName
from auspex.scoring.normalize import (
    blended_percentile_rank,
    blended_zscore,
    mean_std,
    percentile_rank,
    winsorise,
    zscore,
)

DIRECTION_UP_THRESHOLD = Decimal("0.15")
DIRECTION_DOWN_THRESHOLD = Decimal("-0.15")

REASON_RAW_MISSING = "raw_value_missing"
REASON_DEGENERATE_CROSS_SECTION = "degenerate_cross_section"
REASON_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class LegCompositeResult:
    raw: Decimal | None
    z: Decimal | None
    weight: Decimal
    contribution: Decimal | None
    computable: bool
    applicable: bool = True
    reason_not_computable: str | None = None


@dataclass(frozen=True)
class CompositeResult:
    legs: dict[LegName, LegCompositeResult]
    composite: Decimal | None
    weight_sum: Decimal
    computable_weight: Decimal = Decimal(0)


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


def _tier_values(raw_by_leg: dict[LegName, dict[str, Decimal | None]] | None, leg: LegName) -> list[Decimal]:
    if not raw_by_leg:
        return []
    return [v for v in raw_by_leg.get(leg, {}).values() if v is not None]


def compute_security_composite(
    leg_raw_by_leg: dict[LegName, Decimal | None],
    cohort_raw_by_leg: dict[LegName, dict[str, Decimal | None]],
    weights: dict[LegName, Decimal],
    security_id: str,
    winsor_sigma: Decimal = Decimal("2.5"),
    *,
    not_applicable_legs: frozenset[LegName] | None = None,
    parent_raw_by_leg: dict[LegName, dict[str, Decimal | None]] | None = None,
    universe_raw_by_leg: dict[LegName, dict[str, Decimal | None]] | None = None,
    lambda_cohort: Decimal = Decimal(1),
    lambda_parent: Decimal = Decimal(1),
) -> CompositeResult:
    """Compute one security's composite given the full cohort's raw leg values.

    ``cohort_raw_by_leg[leg]`` must include ``security_id`` itself alongside
    its cohort peers so the z-score is computed against the correct
    cross-section. ``parent_raw_by_leg``/``universe_raw_by_leg`` are optional
    wider cross-sections; when supplied with the scope's shrinkage lambdas the
    z-score is a credibility-weighted blend across the three tiers, so a cohort
    that gains or loses members moves scores continuously.
    """

    excluded = not_applicable_legs or frozenset()
    leg_results: dict[LegName, LegCompositeResult] = {}
    weighted_sum = Decimal(0)
    applicable_weight = Decimal(0)
    computable_weight = Decimal(0)

    for leg, weight in weights.items():
        raw = leg_raw_by_leg.get(leg)

        if leg in excluded:
            leg_results[leg] = LegCompositeResult(
                raw=raw,
                z=None,
                weight=Decimal(0),
                contribution=None,
                computable=False,
                applicable=False,
                reason_not_computable=REASON_NOT_APPLICABLE,
            )
            continue

        applicable_weight += weight

        if raw is None:
            leg_results[leg] = LegCompositeResult(
                raw=None,
                z=None,
                weight=weight,
                contribution=Decimal(0),
                computable=False,
                reason_not_computable=REASON_RAW_MISSING,
            )
            continue

        z = blended_zscore(
            raw,
            cohort_values=_tier_values(cohort_raw_by_leg, leg),
            parent_values=_tier_values(parent_raw_by_leg, leg),
            universe_values=_tier_values(universe_raw_by_leg, leg),
            lambda_cohort=lambda_cohort,
            lambda_parent=lambda_parent,
        )

        if z is None:
            leg_results[leg] = LegCompositeResult(
                raw=raw,
                z=None,
                weight=weight,
                contribution=Decimal(0),
                computable=False,
                reason_not_computable=REASON_DEGENERATE_CROSS_SECTION,
            )
            continue

        z_w = winsorise(z, winsor_sigma)
        contribution = weight * z_w
        leg_results[leg] = LegCompositeResult(
            raw=raw, z=z_w, weight=weight, contribution=contribution, computable=True
        )
        weighted_sum += contribution
        computable_weight += weight

    composite = weighted_sum / applicable_weight if computable_weight > 0 and applicable_weight > 0 else None
    return CompositeResult(
        legs=leg_results,
        composite=composite,
        weight_sum=applicable_weight,
        computable_weight=computable_weight,
    )


def classify_direction(delta_7d: Decimal | None) -> Direction:
    if delta_7d is None:
        return Direction.STABLE
    if delta_7d > DIRECTION_UP_THRESHOLD:
        return Direction.STRENGTHENING
    if delta_7d < DIRECTION_DOWN_THRESHOLD:
        return Direction.WEAKENING
    return Direction.STABLE


def compute_percentile(
    security_id: str,
    composites: dict[str, Decimal | None],
    *,
    parent_composites: dict[str, Decimal | None] | None = None,
    universe_composites: dict[str, Decimal | None] | None = None,
    lambda_cohort: Decimal = Decimal(1),
    lambda_parent: Decimal = Decimal(1),
) -> int | None:
    own = composites.get(security_id)
    if own is None:
        return None
    population = [v for v in composites.values() if v is not None]
    if parent_composites is None and universe_composites is None:
        return percentile_rank(own, population)
    return blended_percentile_rank(
        own,
        cohort_population=population,
        parent_population=[v for v in (parent_composites or {}).values() if v is not None],
        universe_population=[v for v in (universe_composites or {}).values() if v is not None],
        lambda_cohort=lambda_cohort,
        lambda_parent=lambda_parent,
    )
