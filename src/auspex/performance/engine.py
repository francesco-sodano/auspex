"""Performance measurement orchestrator (arc42 §5.8) — assembles `PerformanceMetric` rows.

Runs weekly (Sunday 03:00 UTC, arc42 §6.1) over the accumulated score/return
history; this module is the pure-computation core the weekly CLI wires to
persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.models.enums import LegName
from auspex.models.performance import PerformanceMetric
from auspex.performance.cohort_quality import cohort_return_dispersion
from auspex.performance.correlation import average_ic, composite_ic_for_date, leg_correlation_matrix, leg_ic_for_date
from auspex.performance.hit_rate import (
    DispositionOutcome,
    SuggestionOutcome,
    disposition_hit_rate,
    suggestion_hit_rate,
)

HORIZONS = (21, 63, 126)


@dataclass(frozen=True)
class DateCrossSection:
    as_of_date: date
    security_ids: list[str]
    percentiles: list[Decimal]
    leg_z_by_leg: dict[LegName, list[Decimal]]
    forward_returns_usd_by_horizon: dict[int, list[Decimal]]


def compute_composite_ic_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        per_date_ics = [
            composite_ic_for_date(cs.percentiles, cs.forward_returns_usd_by_horizon[horizon])
            for cs in cross_sections
            if horizon in cs.forward_returns_usd_by_horizon
        ]
        avg = average_ic(per_date_ics)
        if avg is None:
            continue
        metrics.append(
            PerformanceMetric(
                id=f"composite_ic:{cross_sections[-1].as_of_date.isoformat()}:h{horizon}",
                metric_type="composite_ic",
                as_of_date=cross_sections[-1].as_of_date,
                horizon_days=horizon,
                scope="universe",
                value=str(avg),
                sample_size=len([ic for ic in per_date_ics if ic is not None]),
            )
        )
    return metrics


def compute_leg_ic_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        for leg in LegName:
            per_date_ics = [
                leg_ic_for_date(cs.leg_z_by_leg[leg], cs.forward_returns_usd_by_horizon[horizon])
                for cs in cross_sections
                if leg in cs.leg_z_by_leg and horizon in cs.forward_returns_usd_by_horizon
            ]
            avg = average_ic(per_date_ics)
            if avg is None:
                continue
            metrics.append(
                PerformanceMetric(
                    id=f"leg_ic:{cross_sections[-1].as_of_date.isoformat()}:{leg.value}:h{horizon}",
                    metric_type="leg_ic",
                    as_of_date=cross_sections[-1].as_of_date,
                    horizon_days=horizon,
                    scope=f"leg:{leg.value}",
                    value=str(avg),
                    sample_size=len([ic for ic in per_date_ics if ic is not None]),
                )
            )
    return metrics


def compute_leg_correlation_metrics(
    as_of_date: date, leg_values_by_leg: dict[LegName, list[Decimal]]
) -> list[PerformanceMetric]:
    matrix = leg_correlation_matrix(leg_values_by_leg)
    metrics: list[PerformanceMetric] = []
    seen: set[frozenset] = set()
    for (leg_a, leg_b), corr in matrix.items():
        pair_key = frozenset((leg_a, leg_b))
        if pair_key in seen or corr is None:
            continue
        seen.add(pair_key)
        metrics.append(
            PerformanceMetric(
                id=f"leg_correlation:{as_of_date.isoformat()}:{leg_a.value}:{leg_b.value}",
                metric_type="leg_correlation",
                as_of_date=as_of_date,
                scope=f"{leg_a.value}x{leg_b.value}",
                value=str(corr),
                sample_size=len(next(iter(leg_values_by_leg.values()), [])),
            )
        )
    return metrics


def compute_suggestion_hit_rate_metric(as_of_date: date, outcomes: list[SuggestionOutcome]) -> PerformanceMetric | None:
    rate = suggestion_hit_rate(outcomes)
    if rate is None:
        return None
    return PerformanceMetric(
        id=f"suggestion_hit_rate:{as_of_date.isoformat()}:universe",
        metric_type="suggestion_hit_rate",
        as_of_date=as_of_date,
        horizon_days=126,
        scope="universe",
        value=str(rate),
        sample_size=len(outcomes),
    )


def compute_disposition_outcome_metric(
    as_of_date: date,
    outcomes: list[DispositionOutcome],
    *,
    accepted: bool,
) -> PerformanceMetric | None:
    rate = disposition_hit_rate(outcomes, accepted=accepted)
    if rate is None:
        return None
    subset = [outcome for outcome in outcomes if outcome.accepted == accepted]
    scope = "accepted" if accepted else "rejected"
    return PerformanceMetric(
        id=f"disposition_outcome:{as_of_date.isoformat()}:{scope}",
        metric_type="disposition_outcome",
        as_of_date=as_of_date,
        horizon_days=126,
        scope=scope,
        value=str(rate),
        sample_size=len(subset),
    )


def compute_cohort_quality_metrics(
    as_of_date: date, returns_by_cohort: dict[str, list[Decimal]]
) -> list[PerformanceMetric]:
    metrics: list[PerformanceMetric] = []
    for cohort, returns in returns_by_cohort.items():
        dispersion = cohort_return_dispersion(returns)
        if dispersion is None:
            continue
        metrics.append(
            PerformanceMetric(
                id=f"cohort_quality:{as_of_date.isoformat()}:{cohort}",
                metric_type="cohort_quality",
                as_of_date=as_of_date,
                scope=f"cohort:{cohort}",
                value=str(dispersion),
                sample_size=len(returns),
            )
        )
    return metrics
