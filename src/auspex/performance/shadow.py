"""Pre-registered champion/challenger shadow validation (arc42 §5.8).

Shadow evaluation answers one question: *would a different weighting have
scored better on data we have already seen?* It answers it under
pre-registration, because the alternative — trying weightings until one wins —
manufactures a winner from noise on any finite history.

A :class:`PreRegistration` fixes the hypothesis, the primary metric, the
horizons, the resampling seed, the competing variants and the decision rule
*before* any evaluation runs, and hashes them into a fingerprint that is
published alongside every result. A report whose fingerprint does not match the
registration it claims to test is evidence of exactly the drift the mechanism
exists to prevent.

Nothing here writes to the ``scores`` container or mutates production weights.
Variants are re-derived from stored leg z-scores in memory; the champion is the
production score exactly as it was published. The output is a set of
``shadow_comparison`` performance metrics, and publishing them is opt-in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from auspex.models.common import content_hash
from auspex.models.enums import LegName
from auspex.models.performance import PerformanceMetric
from auspex.performance.detail import DETAILED_METRICS_VERSION, decimal_str, detail_payload
from auspex.performance.distribution import ic_distribution
from auspex.performance.ic import spearman_ic
from auspex.performance.intervals import DEFAULT_CONFIDENCE, block_bootstrap_interval, newey_west_interval
from auspex.performance.multiple_testing import benjamini_hochberg
from auspex.performance.stats import (
    ZERO,
    effective_sample_size,
    mean,
    sample_std,
    seed_from_text,
)

CHAMPION = "champion"
CORRECTED_FIXED = "corrected_fixed"

SHADOW_METRIC_TYPE = "shadow_comparison"

DEFAULT_HORIZONS = (21, 63, 126)
DEFAULT_MINIMUM_DATES = 12

#: Production weights as published in ``config/weights.yaml`` for domestic filers.
#: Duplicated here deliberately: a shadow study must be reproducible from its own
#: registration long after the live configuration has moved on. Any drift is
#: caught by :func:`assert_matches_production_weights`.
PRODUCTION_DOMESTIC_WEIGHTS: dict[LegName, Decimal] = {
    LegName.THESIS_LINKAGE: Decimal("0.20"),
    LegName.ATTENTION_ACCELERATION: Decimal("0.15"),
    LegName.NARRATIVE_PREMIUM: Decimal("0.10"),
    LegName.SMART_MONEY: Decimal("0.20"),
    LegName.FUNDAMENTAL_HEALTH: Decimal("0.20"),
    LegName.VALUATION_BRAKE: Decimal("0.15"),
}


@dataclass(frozen=True)
class ShadowVariant:
    """One competitor in a shadow study.

    ``weights is None`` means "use the score as it was published" — that is the
    champion, and it is the only variant that is not re-derived.

    ``renormalise_on_computable`` selects the denominator. Production divides by
    the *applicable* weight, so an applicable-but-missing leg contributes a
    neutral zero and dilutes the score. Dividing by the *computable* weight
    instead scores each security on the evidence it actually has. Which is
    better is an empirical question, which is the point of measuring it.
    """

    name: str
    description: str
    weights: dict[LegName, Decimal] | None = None
    renormalise_on_computable: bool = False

    def registration_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "weights": (
                None
                if self.weights is None
                else {leg.value: decimal_str(weight) for leg, weight in sorted(self.weights.items(), key=_leg_key)}
            ),
            "renormalise_on_computable": self.renormalise_on_computable,
        }


CHAMPION_VARIANT = ShadowVariant(
    name=CHAMPION,
    description="Production score exactly as published; no re-derivation.",
    weights=None,
)

CORRECTED_FIXED_VARIANT = ShadowVariant(
    name=CORRECTED_FIXED,
    description=(
        "Production weights with applicable-but-missing legs contributing a "
        "neutral zero while their weight remains in the denominator, compared "
        "with the v4.1 champion that renormalised over computable legs."
    ),
    weights=dict(PRODUCTION_DOMESTIC_WEIGHTS),
    renormalise_on_computable=False,
)


def _leg_key(item: tuple[LegName, Decimal]) -> str:
    return item[0].value


def assert_matches_production_weights(live_weights: dict[LegName, Decimal]) -> None:
    """Fail loudly if the registered snapshot has drifted from live configuration.

    Callers that have the live weight table on hand should pass it; a silent
    mismatch would make ``corrected_fixed`` a comparison against a weighting
    that is no longer in production.
    """

    snapshot = {leg.value: str(weight) for leg, weight in PRODUCTION_DOMESTIC_WEIGHTS.items()}
    live = {leg.value: str(Decimal(str(weight))) for leg, weight in live_weights.items()}
    if snapshot != live:
        raise ValueError(
            "shadow production weight snapshot has drifted from live configuration: "
            f"snapshot={snapshot} live={live}"
        )


@dataclass(frozen=True)
class ShadowCrossSection:
    """One measurement date, carrying everything a variant needs to be re-scored."""

    as_of_date: date
    champion_scores_by_security: dict[str, Decimal]
    leg_z_by_security: dict[str, dict[LegName, Decimal]]
    forward_returns_usd_by_horizon: dict[int, dict[str, Decimal]]
    applicable_legs_by_security: dict[str, frozenset[LegName]] = field(default_factory=dict)

    def applicable_legs(self, security_id: str) -> frozenset[LegName]:
        """Legs that structurally apply, defaulting to every weighted leg.

        A leg absent from ``applicable_legs_by_security`` is treated as
        applicable; the FPI/``SMART_MONEY`` exclusion must be supplied
        explicitly, because guessing it would change the denominator.
        """

        return self.applicable_legs_by_security.get(security_id, frozenset(LegName))


def score_variant(variant: ShadowVariant, cross_section: ShadowCrossSection) -> dict[str, Decimal]:
    """Re-derive one variant's scores for one date."""

    if variant.weights is None:
        return dict(cross_section.champion_scores_by_security)

    scores: dict[str, Decimal] = {}
    for security_id, leg_z in cross_section.leg_z_by_security.items():
        applicable = cross_section.applicable_legs(security_id)
        numerator = ZERO
        applicable_weight = ZERO
        computable_weight = ZERO
        for leg, weight in variant.weights.items():
            if leg not in applicable:
                continue
            applicable_weight += weight
            z = leg_z.get(leg)
            if z is None:
                continue
            numerator += weight * z
            computable_weight += weight
        denominator = computable_weight if variant.renormalise_on_computable else applicable_weight
        if computable_weight <= ZERO or denominator <= ZERO:
            continue
        scores[security_id] = numerator / denominator
    return scores


@dataclass(frozen=True)
class PreRegistration:
    """The study as declared before it was run.

    ``fingerprint`` covers every field that could change a result. Publishing it
    with the metrics is what makes "we pre-registered this" checkable rather
    than asserted.
    """

    study_id: str
    hypothesis: str
    primary_metric: str
    decision_rule: str
    variants: tuple[ShadowVariant, ...]
    registered_on: date
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    seed_text: str = "auspex-shadow"
    confidence: Decimal = DEFAULT_CONFIDENCE
    minimum_dates: int = DEFAULT_MINIMUM_DATES
    minimum_effective_observations: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        names = [variant.name for variant in self.variants]
        if len(names) != len(set(names)):
            raise ValueError(f"shadow variant names must be unique: {names}")
        if CHAMPION not in names:
            raise ValueError("a shadow study must include the champion variant as its baseline")
        if not self.horizons:
            raise ValueError("a shadow study must declare at least one horizon")

    @property
    def variant_names(self) -> tuple[str, ...]:
        return tuple(variant.name for variant in self.variants)

    def registration_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "hypothesis": self.hypothesis,
            "primary_metric": self.primary_metric,
            "decision_rule": self.decision_rule,
            "registered_on": self.registered_on.isoformat(),
            "horizons": list(self.horizons),
            "seed_text": self.seed_text,
            "confidence": decimal_str(self.confidence),
            "minimum_dates": self.minimum_dates,
            "minimum_effective_observations": decimal_str(
                self.minimum_effective_observations
            ),
            "variants": [variant.registration_payload() for variant in self.variants],
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(json.dumps(self.registration_payload(), sort_keys=True, separators=(",", ":")))


def default_pre_registration(registered_on: date, *, challengers: tuple[ShadowVariant, ...] = ()) -> PreRegistration:
    """Standing study: does neutral missing-leg treatment beat the old champion?"""

    return PreRegistration(
        study_id="shadow-v4.2-neutral-missing-v1",
        hypothesis=(
            "The v4.2 neutral missing-leg formula produces a higher mean rank "
            "information coefficient than the stored v4.1 champion, which "
            "renormalised each security over only its computable legs."
        ),
        primary_metric="mean_composite_ic_h126",
        decision_rule=(
            "Promote a challenger only if its mean IC exceeds the champion's at "
            "the 126-session horizon, the paired difference interval excludes "
            "zero, and the Benjamini-Hochberg q-value across all variant-horizon "
            "comparisons is at or below 0.05."
        ),
        variants=(CHAMPION_VARIANT, CORRECTED_FIXED_VARIANT, *challengers),
        registered_on=registered_on,
    )


@dataclass(frozen=True)
class VariantResult:
    """One variant at one horizon."""

    variant: str
    horizon_days: int
    dates_used: int
    matched_observations: int
    mean_ic: Decimal | None
    std_ic: Decimal | None
    icir: Decimal | None
    effective_sample_size: Decimal | None
    p_value: Decimal | None
    per_date_ics: dict[date, Decimal]


@dataclass(frozen=True)
class VariantComparison:
    """One challenger measured against the champion, paired by date."""

    variant: str
    horizon_days: int
    dates_used: int
    mean_difference: Decimal | None
    std_difference: Decimal | None
    win_fraction: Decimal | None
    interval_low: Decimal | None
    interval_high: Decimal | None
    excludes_zero: bool | None
    bootstrap_low: Decimal | None
    bootstrap_high: Decimal | None
    p_value: Decimal | None
    q_value: Decimal | None
    rejected: bool | None
    seed: int


@dataclass(frozen=True)
class ShadowReport:
    registration: PreRegistration
    fingerprint: str
    dates_evaluated: int
    underpowered: bool
    results: tuple[VariantResult, ...]
    comparisons: tuple[VariantComparison, ...]

    @property
    def as_of_date(self) -> date | None:
        dates = [day for result in self.results for day in result.per_date_ics]
        return max(dates) if dates else None


def _variant_ic_series(
    variant: ShadowVariant,
    cross_sections: list[ShadowCrossSection],
    horizon: int,
    common_ids_by_date: dict[date, frozenset[str]],
) -> tuple[dict[date, Decimal], int]:
    series: dict[date, Decimal] = {}
    matched = 0
    for cs in cross_sections:
        returns = cs.forward_returns_usd_by_horizon.get(horizon)
        if not returns:
            continue
        scores = score_variant(variant, cs)
        shared = sorted(
            set(scores)
            & set(returns)
            & set(common_ids_by_date.get(cs.as_of_date, frozenset()))
        )
        if len(shared) < 2:
            continue
        value = spearman_ic([scores[sid] for sid in shared], [returns[sid] for sid in shared])
        if value is None:
            continue
        series[cs.as_of_date] = value
        matched += len(shared)
    return series, matched


def run_shadow_comparison(
    registration: PreRegistration,
    cross_sections: list[ShadowCrossSection],
) -> ShadowReport:
    """Evaluate every registered variant against the champion.

    Deterministic end to end: the bootstrap seed is derived from the
    registration's ``seed_text`` plus the variant and horizon, so re-running the
    same study on the same data reproduces the same intervals exactly.
    """

    ordered = sorted(cross_sections, key=lambda cs: cs.as_of_date)
    scores_by_variant_date = {
        (variant.name, cross_section.as_of_date): score_variant(
            variant,
            cross_section,
        )
        for variant in registration.variants
        for cross_section in ordered
    }

    results: list[VariantResult] = []
    series_by_variant: dict[tuple[str, int], dict[date, Decimal]] = {}
    for variant in registration.variants:
        for horizon in registration.horizons:
            common_ids_by_date = {}
            for cross_section in ordered:
                returns = cross_section.forward_returns_usd_by_horizon.get(
                    horizon,
                    {},
                )
                common = set(returns)
                for registered_variant in registration.variants:
                    common &= set(
                        scores_by_variant_date.get(
                            (
                                registered_variant.name,
                                cross_section.as_of_date,
                            ),
                            {},
                        )
                    )
                common_ids_by_date[cross_section.as_of_date] = frozenset(
                    common
                )
            series, matched = _variant_ic_series(
                variant,
                ordered,
                horizon,
                common_ids_by_date,
            )
            series_by_variant[(variant.name, horizon)] = series
            distribution = ic_distribution(list(series.values()), horizon)
            results.append(
                VariantResult(
                    variant=variant.name,
                    horizon_days=horizon,
                    dates_used=len(series),
                    matched_observations=matched,
                    mean_ic=None if distribution is None else distribution.mean,
                    std_ic=None if distribution is None else distribution.std,
                    icir=None if distribution is None else distribution.icir,
                    effective_sample_size=None if distribution is None else distribution.effective_sample_size,
                    p_value=None if distribution is None else distribution.p_value,
                    per_date_ics=series,
                )
            )

    raw_comparisons: list[VariantComparison] = []
    p_values: dict[str, Decimal] = {}
    for variant in registration.variants:
        if variant.name == CHAMPION:
            continue
        for horizon in registration.horizons:
            champion_series = series_by_variant.get((CHAMPION, horizon), {})
            challenger_series = series_by_variant.get((variant.name, horizon), {})
            shared = sorted(set(champion_series) & set(challenger_series))
            differences = [challenger_series[day] - champion_series[day] for day in shared]
            seed = seed_from_text(f"{registration.seed_text}:{registration.study_id}:{variant.name}:{horizon}")

            interval = newey_west_interval(differences, horizon_days=horizon, confidence=registration.confidence)
            bootstrap = block_bootstrap_interval(
                differences,
                horizon_days=horizon,
                seed=seed,
                confidence=registration.confidence,
            )
            wins = sum(1 for value in differences if value > ZERO)
            label = f"{variant.name}:h{horizon}"
            distribution = ic_distribution(differences, horizon)
            if distribution is not None and distribution.p_value is not None:
                p_values[label] = distribution.p_value

            raw_comparisons.append(
                VariantComparison(
                    variant=variant.name,
                    horizon_days=horizon,
                    dates_used=len(shared),
                    mean_difference=mean(differences),
                    std_difference=sample_std(differences),
                    win_fraction=(Decimal(wins) / Decimal(len(differences)) if differences else None),
                    interval_low=None if interval is None else interval.low,
                    interval_high=None if interval is None else interval.high,
                    excludes_zero=None if interval is None else interval.excludes_zero,
                    bootstrap_low=None if bootstrap is None else bootstrap.low,
                    bootstrap_high=None if bootstrap is None else bootstrap.high,
                    p_value=None if distribution is None else distribution.p_value,
                    q_value=None,
                    rejected=None,
                    seed=seed,
                )
            )

    adjusted = {result.label: result for result in benjamini_hochberg(p_values)}
    comparisons = tuple(
        VariantComparison(
            variant=comparison.variant,
            horizon_days=comparison.horizon_days,
            dates_used=comparison.dates_used,
            mean_difference=comparison.mean_difference,
            std_difference=comparison.std_difference,
            win_fraction=comparison.win_fraction,
            interval_low=comparison.interval_low,
            interval_high=comparison.interval_high,
            excludes_zero=comparison.excludes_zero,
            bootstrap_low=comparison.bootstrap_low,
            bootstrap_high=comparison.bootstrap_high,
            p_value=comparison.p_value,
            q_value=(
                adjusted[f"{comparison.variant}:h{comparison.horizon_days}"].q_value
                if f"{comparison.variant}:h{comparison.horizon_days}" in adjusted
                else None
            ),
            rejected=(
                adjusted[f"{comparison.variant}:h{comparison.horizon_days}"].rejected
                if f"{comparison.variant}:h{comparison.horizon_days}" in adjusted
                else None
            ),
            seed=comparison.seed,
        )
        for comparison in raw_comparisons
    )

    dates_evaluated = len({cs.as_of_date for cs in ordered})
    return ShadowReport(
        registration=registration,
        fingerprint=registration.fingerprint,
        dates_evaluated=dates_evaluated,
        underpowered=dates_evaluated < registration.minimum_dates,
        results=tuple(results),
        comparisons=comparisons,
    )


def promotion_verdict(report: ShadowReport, comparison: VariantComparison) -> str:
    """Apply the registered decision rule to one comparison.

    Returns ``insufficient_evidence`` unless every registered criterion holds
    simultaneously — an underpowered study can never return ``promote``, which
    is the whole reason ``minimum_dates`` is registered up front.
    """

    primary_horizon = int(
        report.registration.primary_metric.rsplit("h", 1)[-1]
    )
    if comparison.horizon_days != primary_horizon:
        return "not_primary"
    if (
        report.underpowered
        or comparison.mean_difference is None
        or effective_sample_size(
            comparison.dates_used,
            comparison.horizon_days,
        )
        < report.registration.minimum_effective_observations
    ):
        return "insufficient_evidence"
    if comparison.mean_difference <= ZERO:
        return "no_improvement"
    if not comparison.excludes_zero:
        return "insufficient_evidence"
    if comparison.rejected is not True:
        return "insufficient_evidence"
    return "promote"


def shadow_metrics(report: ShadowReport) -> list[PerformanceMetric]:
    """Render a report as ``shadow_comparison`` rows.

    These are measurement output only. Nothing downstream reads them to score
    or rank securities; promotion remains a human decision taken against the
    registered rule.
    """

    as_of_date = report.as_of_date
    if as_of_date is None:
        return []

    study = report.registration.study_id
    metrics: list[PerformanceMetric] = []

    for result in report.results:
        if result.mean_ic is None:
            continue
        metrics.append(
            PerformanceMetric(
                id=f"{SHADOW_METRIC_TYPE}:{as_of_date.isoformat()}:{study}:{result.variant}:h{result.horizon_days}",
                metric_type=SHADOW_METRIC_TYPE,
                as_of_date=as_of_date,
                horizon_days=result.horizon_days,
                scope=f"variant:{result.variant}",
                value=str(result.mean_ic),
                sample_size=result.dates_used,
                detail=detail_payload(
                    study_id=study,
                    fingerprint=report.fingerprint,
                    std_ic=result.std_ic,
                    icir=result.icir,
                    effective_sample_size=result.effective_sample_size,
                    p_value=result.p_value,
                    matched_observations=result.matched_observations,
                    dates_evaluated=report.dates_evaluated,
                    underpowered=report.underpowered,
                ),
                metrics_version=DETAILED_METRICS_VERSION,
            )
        )

    for comparison in report.comparisons:
        if comparison.mean_difference is None:
            continue
        scope = f"vs_champion:{comparison.variant}"
        metrics.append(
            PerformanceMetric(
                id=(
                    f"{SHADOW_METRIC_TYPE}:{as_of_date.isoformat()}:{study}:"
                    f"vs_champion:{comparison.variant}:h{comparison.horizon_days}"
                ),
                metric_type=SHADOW_METRIC_TYPE,
                as_of_date=as_of_date,
                horizon_days=comparison.horizon_days,
                scope=scope,
                value=str(comparison.mean_difference),
                sample_size=comparison.dates_used,
                detail=detail_payload(
                    study_id=study,
                    fingerprint=report.fingerprint,
                    std_difference=comparison.std_difference,
                    win_fraction=comparison.win_fraction,
                    interval_low=comparison.interval_low,
                    interval_high=comparison.interval_high,
                    excludes_zero=comparison.excludes_zero,
                    bootstrap_low=comparison.bootstrap_low,
                    bootstrap_high=comparison.bootstrap_high,
                    p_value=comparison.p_value,
                    q_value=comparison.q_value,
                    rejected=comparison.rejected,
                    seed=comparison.seed,
                    verdict=promotion_verdict(report, comparison),
                    underpowered=report.underpowered,
                ),
                metrics_version=DETAILED_METRICS_VERSION,
            )
        )

    return metrics
