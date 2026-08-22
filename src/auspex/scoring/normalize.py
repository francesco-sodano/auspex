"""Cross-sectional normalisation: z-score, winsorisation, cohort fallback ladder.

arc42 §5.5 "Cohort normalisation" / "Composite". Pure functions, ``Decimal``
throughout, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.currency.money import to_decimal
from auspex.models.enums import CohortConfidence

#: The *single* authoritative cohort/parent size thresholds. Everything else
#: about the fallback ladder — the confidence lambdas below, and therefore the
#: reported scope label — is derived from these two numbers through
#: :func:`shrinkage_lambda`. Editing one of them cannot silently disagree with a
#: separately maintained lambda constant, which is exactly the failure mode a
#: pair of "documentation only" constants invited.
COHORT_MIN_SIZE = 12
PARENT_MIN_SIZE = 8

#: Credibility constant for the continuous shrinkage blend. Chosen so the two
#: documented ladder thresholds fall out of the same formula exactly:
#: ``lambda(12) = 12/(12+12) = 0.5`` and ``lambda(8) = 8/(8+12) = 0.4``.
SHRINKAGE_K = 12

#: A cross-section needs at least this many observations before a z-score or a
#: percentile computed against it means anything.
MIN_CROSS_SECTION = 2


def shrinkage_lambda(n: int, k: int = SHRINKAGE_K) -> Decimal:
    """Credibility weight ``n / (n + k)`` for a cross-section of size ``n``.

    Continuous and monotone in ``n``: a cohort that gains or loses one member
    moves its own weight by a small amount instead of switching scope wholesale,
    which is what removes the fallback-ladder cliff (arc42 §5.5 "Cohort
    normalisation" — the ladder's *labels* are preserved, its discontinuity is not).
    """

    if n <= 0:
        return Decimal(0)
    return Decimal(n) / Decimal(n + k)


#: Derived, never hand-maintained: ``lambda_cohort >= HIGH_CONFIDENCE_LAMBDA``
#: is by construction the same predicate as ``n_cohort >= COHORT_MIN_SIZE``.
HIGH_CONFIDENCE_LAMBDA = shrinkage_lambda(COHORT_MIN_SIZE)
MEDIUM_CONFIDENCE_LAMBDA = shrinkage_lambda(PARENT_MIN_SIZE)


@dataclass(frozen=True)
class CohortScope:
    """Scope label plus the full tier structure used for shrinkage blending.

    ``scope``/``member_ids`` keep the historical fallback-ladder meaning (the
    tier whose *label* is reported), while the explicit per-tier member lists and
    lambdas describe the continuous blend that is actually applied to statistics.
    """

    scope: str
    confidence: CohortConfidence
    member_ids: tuple[str, ...]
    cohort_name: str = ""
    parent_name: str = ""
    cohort_member_ids: tuple[str, ...] = ()
    parent_member_ids: tuple[str, ...] = ()
    universe_member_ids: tuple[str, ...] = ()
    lambda_cohort: Decimal = Decimal(1)
    lambda_parent: Decimal = Decimal(1)


def assign_cohort_scope(
    *,
    cohort_name: str,
    cohort_member_ids: list[str],
    parent_name: str,
    parent_member_ids: list[str],
    universe_member_ids: list[str],
) -> CohortScope:
    """Assign the reported scope label and the continuous shrinkage weights.

    The reported ``scope``/``confidence`` reproduce the documented three-level
    ladder exactly (``lambda_cohort >= 0.5`` is identical to ``n_cohort >= 12``,
    ``lambda_parent >= 0.4`` to ``n_parent >= 8``), so the label a user sees is
    unchanged. Statistics, however, are blended across all three tiers using
    ``lambda_cohort``/``lambda_parent`` so that a cohort crossing a threshold
    shifts scores continuously instead of stepping.

    ``cohort_member_ids`` etc. must already exclude any security removed for
    staleness that day (arc42 §5.5 "Staleness exclusion" happens upstream).
    """

    lambda_cohort = shrinkage_lambda(len(cohort_member_ids))
    lambda_parent = shrinkage_lambda(len(parent_member_ids))

    if lambda_cohort >= HIGH_CONFIDENCE_LAMBDA:
        scope, confidence, members = cohort_name, CohortConfidence.HIGH, cohort_member_ids
    elif lambda_parent >= MEDIUM_CONFIDENCE_LAMBDA:
        scope, confidence, members = parent_name, CohortConfidence.MEDIUM, parent_member_ids
    else:
        scope, confidence, members = "universe", CohortConfidence.LOW, universe_member_ids

    return CohortScope(
        scope=scope,
        confidence=confidence,
        member_ids=tuple(members),
        cohort_name=cohort_name,
        parent_name=parent_name,
        cohort_member_ids=tuple(cohort_member_ids),
        parent_member_ids=tuple(parent_member_ids),
        universe_member_ids=tuple(universe_member_ids),
        lambda_cohort=lambda_cohort,
        lambda_parent=lambda_parent,
    )


def shrinkage_tier_weights(lambda_cohort: Decimal, lambda_parent: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Credibility split across (cohort, parent, universe); always sums to 1."""

    cohort_weight = lambda_cohort
    remainder = Decimal(1) - lambda_cohort
    parent_weight = remainder * lambda_parent
    universe_weight = remainder * (Decimal(1) - lambda_parent)
    return cohort_weight, parent_weight, universe_weight


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


def percentile_rank_fraction(value: Decimal, population: list[Decimal]) -> Decimal | None:
    """Tie-aware midpoint percentile of ``value`` within ``population``, in [0, 1].

    Uses the *midpoint* (a.k.a. mean/Hazen) convention
    ``(below + 0.5 * ties) / n`` rather than "fraction at or below". Two
    consequences matter for correctness:

    * ties share one rank instead of the later-sorted member claiming credit for
      the whole tie group, and
    * the endpoints are never 0% or 100%, so a three-member cohort spans
      16.7/50/83.3 instead of 33/67/100. A tiny cohort can no longer manufacture
      a "top of the market" reading for its best member.

    Returns ``None`` for an empty population (non-computable, never 0).
    """

    if not population:
        return None
    below = sum(1 for v in population if v < value)
    ties = sum(1 for v in population if v == value)
    return (Decimal(below) + Decimal(ties) / Decimal(2)) / Decimal(len(population))


def percentile_rank(value: Decimal, population: list[Decimal]) -> int:
    """Midpoint/tie-aware percentile rank of ``value`` in ``population``, 0-100."""

    fraction = percentile_rank_fraction(value, population)
    if fraction is None:
        return 0
    pct = fraction * Decimal(100)
    return int(pct.to_integral_value(rounding="ROUND_HALF_UP"))


def blended_zscore(
    value: Decimal,
    *,
    cohort_values: list[Decimal],
    parent_values: list[Decimal] | None = None,
    universe_values: list[Decimal] | None = None,
    lambda_cohort: Decimal = Decimal(1),
    lambda_parent: Decimal = Decimal(1),
) -> Decimal | None:
    """Credibility-weighted blend of the cohort / parent / universe z-scores.

    Each tier contributes its own z-score weighted by
    :func:`shrinkage_tier_weights`. A tier that cannot produce a z-score (too
    few observations, or a degenerate constant cross-section) simply drops out
    and the remaining tier weights are rescaled, so the blend degrades smoothly
    instead of falling off a cliff. Returns ``None`` when no tier is usable.
    """

    cohort_weight, parent_weight, universe_weight = shrinkage_tier_weights(lambda_cohort, lambda_parent)
    candidates: tuple[tuple[Decimal, list[Decimal]], ...] = (
        (cohort_weight, cohort_values),
        (parent_weight, parent_values or []),
        (universe_weight, universe_values or []),
    )

    total = Decimal(0)
    weight_used = Decimal(0)
    for weight, values in candidates:
        if weight <= 0 or len(values) < MIN_CROSS_SECTION:
            continue
        mean, std = mean_std(values)
        if mean is None or std is None:
            continue
        z = zscore(value, mean, std)
        if z is None:
            continue
        total += weight * z
        weight_used += weight

    if weight_used == 0:
        return None
    return total / weight_used


def blended_percentile_rank(
    value: Decimal,
    *,
    cohort_population: list[Decimal],
    parent_population: list[Decimal] | None = None,
    universe_population: list[Decimal] | None = None,
    lambda_cohort: Decimal = Decimal(1),
    lambda_parent: Decimal = Decimal(1),
) -> int | None:
    """Shrinkage-blended midpoint percentile across the three cohort tiers."""

    cohort_weight, parent_weight, universe_weight = shrinkage_tier_weights(lambda_cohort, lambda_parent)
    candidates: tuple[tuple[Decimal, list[Decimal]], ...] = (
        (cohort_weight, cohort_population),
        (parent_weight, parent_population or []),
        (universe_weight, universe_population or []),
    )

    total = Decimal(0)
    weight_used = Decimal(0)
    for weight, population in candidates:
        if weight <= 0 or not population:
            continue
        fraction = percentile_rank_fraction(value, population)
        if fraction is None:
            continue
        total += weight * fraction
        weight_used += weight

    if weight_used == 0:
        return None
    pct = (total / weight_used) * Decimal(100)
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
