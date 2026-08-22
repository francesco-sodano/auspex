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
from decimal import ROUND_HALF_EVEN, Decimal

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
class LegCrossSection:
    """The exact peer cross-section one leg's z-score was computed against.

    Retained so a later step can re-evaluate *a different raw value* against the
    very same distribution. That counterfactual is what makes the leg-change
    attribution in ``leg_changes`` an identity rather than an assertion: holding
    this cross-section fixed and moving only the security's own raw value
    isolates the own-evidence half of ``delta_z`` exactly (see
    :func:`auspex.scoring.composite.decompose_leg_delta`).
    """

    cohort_values: tuple[Decimal, ...] = ()
    parent_values: tuple[Decimal, ...] = ()
    universe_values: tuple[Decimal, ...] = ()
    lambda_cohort: Decimal = Decimal(1)
    lambda_parent: Decimal = Decimal(1)
    winsor_sigma: Decimal = Decimal("2.5")

    def z_for(self, raw: Decimal) -> Decimal | None:
        """Winsorised blended z of ``raw`` against this fixed cross-section.

        Winsorised because the reported ``z`` on every score row is winsorised;
        the counterfactual has to live on the same scale or the decomposition
        would not add up to the reported delta.
        """

        z = blended_zscore(
            raw,
            cohort_values=list(self.cohort_values),
            parent_values=list(self.parent_values),
            universe_values=list(self.universe_values),
            lambda_cohort=self.lambda_cohort,
            lambda_parent=self.lambda_parent,
        )
        if z is None:
            return None
        return winsorise(z, self.winsor_sigma)


@dataclass(frozen=True)
class LegCompositeResult:
    raw: Decimal | None
    z: Decimal | None
    weight: Decimal
    contribution: Decimal | None
    computable: bool
    applicable: bool = True
    reason_not_computable: str | None = None
    #: The cross-section ``z`` was computed against; ``None`` for a leg that is
    #: structurally not applicable and therefore never had one.
    cross_section: LegCrossSection | None = None


@dataclass(frozen=True)
class LegDeltaDecomposition:
    """Exact two-term split of ``delta_z`` (arc42 §5.5 "Leg change").

    ``own_evidence_effect + cohort_distribution_effect == delta_z`` by
    construction, or every field is ``None``. There is deliberately no third
    "residual" term and no partial answer: an attribution that cannot be
    computed is reported as unavailable rather than silently collapsed onto the
    own-evidence term, which would tell a user their issuer moved when in fact
    the peer group did.
    """

    delta_z: Decimal | None
    own_evidence_effect: Decimal | None
    cohort_distribution_effect: Decimal | None
    reason_unavailable: str | None = None


REASON_DECOMPOSITION_NO_PRIOR = "no_prior_leg_value"
REASON_DECOMPOSITION_NO_CURRENT = "no_current_leg_value"
REASON_DECOMPOSITION_NO_CROSS_SECTION = "prior_value_not_rankable_in_current_cross_section"

#: Attribution is reported in fixed point to this many decimal places.
#:
#: ``Decimal`` arithmetic is *floating* — every operation rounds to 28
#: significant digits — so two independently computed differences can disagree
#: with their own total in the last unit in the last place. These three numbers
#: are persisted and read together, and a reader who adds the two published
#: effects must get the published total rather than the total plus a residue.
#: Rounding to a fixed scale first makes the identity hold exactly as stored.
#: z-scores are winsorised to a couple of sigma, so twelve decimal places is
#: several orders of magnitude finer than the signal.
ATTRIBUTION_QUANTUM = Decimal("1E-12")


def decompose_leg_delta(
    *,
    prior_z: Decimal | None,
    current_z: Decimal | None,
    prior_raw: Decimal | None,
    current_cross_section: LegCrossSection | None,
) -> LegDeltaDecomposition:
    """Split ``current_z - prior_z`` into own-evidence and peer-distribution halves.

    Writing ``z(x; D)`` for the winsorised blended z of raw value ``x`` against
    peer distribution ``D``, the reported endpoints are ``z_prior = z(x_p; D_p)``
    and ``z_current = z(x_c; D_c)``. Inserting the counterfactual
    ``z† = z(x_p; D_c)`` — this security's *prior* raw value ranked against
    *today's* peers — gives

    ```
    cohort_distribution_effect = z†        - z_prior     (peers moved, issuer held)
    own_evidence_effect        = z_current - z†          (issuer moved, peers held)
    ```

    which telescopes to ``z_current - z_prior = delta_z`` because the middle term
    cancels. Reported in fixed point (:data:`ATTRIBUTION_QUANTUM`) so the
    identity survives ``Decimal``'s floating rounding and the three published
    numbers reconcile exactly as stored.

    The order matters. Anchoring on the *reported* prior z and re-ranking
    against the *current* cross-section means every difference between
    yesterday's and today's peer group — peer values moving, members joining or
    leaving, the shrinkage lambdas shifting as a result — lands in the
    distribution term, where it belongs. Building the counterfactual the other
    way round (today's raw against a reconstructed prior cross-section) would
    charge all of that reconstruction error to the issuer's own evidence.

    Returns an all-``None`` decomposition with a reason when the leg is not
    computable today, when there is no prior value to move from, or when the
    prior value cannot be ranked against today's cross-section. Those three are
    reported distinctly: "the leg lost its evidence today", "this is the leg's
    first observation" and "today's peer group cannot rank anything" are
    different facts about the row, and collapsing them into one reason forces a
    reader to guess which happened.
    """

    if current_z is None:
        # Checked first: when the leg is non-computable today there is nothing
        # to decompose regardless of history, and ``LegResult`` already carries
        # the specific reason the current value is missing.
        return LegDeltaDecomposition(
            delta_z=None,
            own_evidence_effect=None,
            cohort_distribution_effect=None,
            reason_unavailable=REASON_DECOMPOSITION_NO_CURRENT,
        )

    if prior_z is None:
        return LegDeltaDecomposition(
            delta_z=None,
            own_evidence_effect=None,
            cohort_distribution_effect=None,
            reason_unavailable=REASON_DECOMPOSITION_NO_PRIOR,
        )

    delta = (current_z - prior_z).quantize(ATTRIBUTION_QUANTUM, rounding=ROUND_HALF_EVEN)

    if prior_raw is None:
        return LegDeltaDecomposition(
            delta_z=delta,
            own_evidence_effect=None,
            cohort_distribution_effect=None,
            reason_unavailable=REASON_DECOMPOSITION_NO_PRIOR,
        )

    counterfactual = current_cross_section.z_for(prior_raw) if current_cross_section is not None else None
    if counterfactual is None:
        return LegDeltaDecomposition(
            delta_z=delta,
            own_evidence_effect=None,
            cohort_distribution_effect=None,
            reason_unavailable=REASON_DECOMPOSITION_NO_CROSS_SECTION,
        )

    # ``cohort_distribution_effect`` is algebraically ``counterfactual - prior_z``
    # and is computed as the residual of the fixed-point delta instead, so the
    # three published numbers add up exactly rather than to within a last-digit
    # rounding artefact. Both degenerate cases stay exactly degenerate: peers
    # unchanged gives ``counterfactual == prior_z`` and therefore a distribution
    # effect of exactly zero, and an unchanged raw value gives
    # ``counterfactual == current_z`` and an own-evidence effect of exactly zero.
    own_evidence_effect = (current_z - counterfactual).quantize(
        ATTRIBUTION_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    return LegDeltaDecomposition(
        delta_z=delta,
        own_evidence_effect=own_evidence_effect,
        cohort_distribution_effect=delta - own_evidence_effect,
    )


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

        cross_section = LegCrossSection(
            cohort_values=tuple(_tier_values(cohort_raw_by_leg, leg)),
            parent_values=tuple(_tier_values(parent_raw_by_leg, leg)),
            universe_values=tuple(_tier_values(universe_raw_by_leg, leg)),
            lambda_cohort=lambda_cohort,
            lambda_parent=lambda_parent,
            winsor_sigma=winsor_sigma,
        )

        if raw is None:
            leg_results[leg] = LegCompositeResult(
                raw=None,
                z=None,
                weight=weight,
                contribution=Decimal(0),
                computable=False,
                reason_not_computable=REASON_RAW_MISSING,
                cross_section=cross_section,
            )
            continue

        z = blended_zscore(
            raw,
            cohort_values=list(cross_section.cohort_values),
            parent_values=list(cross_section.parent_values),
            universe_values=list(cross_section.universe_values),
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
                cross_section=cross_section,
            )
            continue

        z_w = winsorise(z, winsor_sigma)
        contribution = weight * z_w
        leg_results[leg] = LegCompositeResult(
            raw=raw,
            z=z_w,
            weight=weight,
            contribution=contribution,
            computable=True,
            cross_section=cross_section,
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
