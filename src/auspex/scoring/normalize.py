"""Cross-sectional normalisation: z-score, winsorisation, cohort fallback ladder.

arc42 §5.5 "Cohort normalisation" / "Composite". Pure functions, ``Decimal``
throughout, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.currency.money import to_decimal
from auspex.models.enums import CohortConfidence

COHORT_MIN_SIZE = 12
PARENT_MIN_SIZE = 8


@dataclass(frozen=True)
class CohortScope:
    scope: str
    confidence: CohortConfidence
    member_ids: tuple[str, ...]


def assign_cohort_scope(
    *,
    cohort_name: str,
    cohort_member_ids: list[str],
    parent_name: str,
    parent_member_ids: list[str],
    universe_member_ids: list[str],
) -> CohortScope:
    """Three-level fallback ladder (arc42 §5.5).

    ``cohort_member_ids`` etc. must already exclude any security removed for
    staleness that day (arc42 §5.5 "Staleness exclusion" happens upstream).
    """

    if len(cohort_member_ids) >= COHORT_MIN_SIZE:
        return CohortScope(cohort_name, CohortConfidence.HIGH, tuple(cohort_member_ids))
    if len(parent_member_ids) >= PARENT_MIN_SIZE:
        return CohortScope(parent_name, CohortConfidence.MEDIUM, tuple(parent_member_ids))
    return CohortScope("universe", CohortConfidence.LOW, tuple(universe_member_ids))


def mean_std(values: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    """Population mean/std of a list of Decimals. Returns (None, None) if empty.

    Uses population standard deviation (divide by N), matching a fixed
    cross-section rather than a sample drawn from a larger population.
    """

    if not values:
        return None, None
    n = Decimal(len(values))
    mean = sum(values, Decimal(0)) / n
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / n
    std = variance.sqrt() if variance > 0 else Decimal(0)
    return mean, std


def zscore(value: Decimal, mean: Decimal, std: Decimal) -> Decimal | None:
    """Return None (non-computable) when std is zero — cannot rank a constant cross-section."""

    if std == 0:
        return None
    return (value - mean) / std


def winsorise(z: Decimal, sigma: Decimal = Decimal("2.5")) -> Decimal:
    if z > sigma:
        return sigma
    if z < -sigma:
        return -sigma
    return z


def percentile_rank(value: Decimal, population: list[Decimal]) -> int:
    """Percentile rank of ``value`` within ``population`` (inclusive of itself), 0-100.

    Uses the "fraction of population <= value" convention, matching common
    practice for cross-sectional composite ranking.
    """

    if not population:
        return 0
    at_or_below = sum(1 for v in population if v <= value)
    pct = (Decimal(at_or_below) / Decimal(len(population))) * Decimal(100)
    return int(pct.to_integral_value(rounding="ROUND_HALF_UP"))


def clip(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


def exponential_decay(age_days: int, half_life_days: Decimal) -> Decimal:
    """``exp(-age_days / half_life)`` computed via Decimal-friendly ``e**x``.

    ``Decimal`` has no native ``exp``; we use ``math.exp`` on the float ratio
    (a pure decay weight, not a monetary value) and immediately re-wrap via
    ``str()`` so the *result* re-enters Decimal arithmetic cleanly. This is the
    one place floats are used, deliberately isolated to a non-monetary decay
    coefficient, and is exact enough for a recency-weighting heuristic.
    """

    import math

    ratio = float(age_days) / float(half_life_days)
    return to_decimal(str(math.exp(-ratio)))
