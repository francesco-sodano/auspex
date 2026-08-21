"""Performance measurement orchestrator (arc42 §5.8) — assembles `PerformanceMetric` rows.

Runs weekly (Sunday 03:00 UTC, arc42 §6.1) over the accumulated score/return
history; this module is the pure-computation core the weekly CLI wires to
persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from auspex.models.enums import LegName
from auspex.models.performance import PerformanceMetric
from auspex.performance.benchmarks import equal_weight_return, momentum_ic, random_null_band
from auspex.performance.cohort_quality import cohort_return_dispersion
from auspex.performance.correlation import average_ic, composite_ic_for_date, leg_correlation_matrix
from auspex.performance.coverage_bias import coverage_bias
from auspex.performance.detail import DETAILED_METRICS_VERSION, decimal_str, detail_payload
from auspex.performance.distribution import ic_distribution
from auspex.performance.hit_rate import (
    DispositionOutcome,
    SuggestionOutcome,
    disposition_hit_rate,
    suggestion_hit_rate,
)
from auspex.performance.intervals import block_bootstrap_interval, newey_west_interval
from auspex.performance.matching import aggregate_pair_correlations, matched_leg_ic
from auspex.performance.multiple_testing import benjamini_hochberg
from auspex.performance.spread import cost_adjusted_return, max_drawdown, top_minus_bottom, turnover
from auspex.performance.stats import ZERO, mean, sample_std, seed_from_text

HORIZONS = (21, 63, 126)

DEFAULT_SEED_TEXT = "auspex-performance"


@dataclass(frozen=True)
class DateCrossSection:
    as_of_date: date
    security_ids: list[str]
    percentiles: list[Decimal]
    leg_z_by_leg: dict[LegName, list[Decimal]]
    forward_returns_usd_by_horizon: dict[int, list[Decimal]]
    leg_z_by_security: dict[LegName, dict[str, Decimal]] = field(default_factory=dict)
    coverage_by_security: dict[str, Decimal] = field(default_factory=dict)
    trailing_returns_usd_by_window: dict[int, dict[str, Decimal]] = field(default_factory=dict)

    @property
    def percentile_by_security(self) -> dict[str, Decimal]:
        return dict(zip(self.security_ids, self.percentiles, strict=False))

    def returns_by_security(self, horizon: int) -> dict[str, Decimal]:
        returns = self.forward_returns_usd_by_horizon.get(horizon)
        if returns is None:
            return {}
        return dict(zip(self.security_ids, returns, strict=False))

    def leg_values_by_security(self, leg: LegName) -> dict[str, Decimal]:
        """Per-leg values on that leg's *available* names.

        Prefers the sparse per-security map. Falls back to the legacy dense
        list, which by construction only exists when every candidate had the
        leg, so the fallback is exact rather than an approximation.
        """

        available = self.leg_z_by_security.get(leg)
        if available is not None:
            return available
        dense = self.leg_z_by_leg.get(leg)
        if dense is None:
            return {}
        return dict(zip(self.security_ids, dense, strict=False))

    @property
    def available_legs(self) -> list[LegName]:
        legs = set(self.leg_z_by_security) | set(self.leg_z_by_leg)
        return sorted(legs, key=lambda leg: leg.value)


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


def _std_or_none(values: list[Decimal]) -> Decimal | None:
    return sample_std(values)


def compute_leg_ic_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    """Per-leg IC over each leg's own matched name/date population.

    Previously a date contributed to a leg only when *every* candidate carried
    that leg, which silently discarded dates in a way correlated with filer
    profile (FPI filers have no ``SMART_MONEY``). Each leg now uses the names it
    actually covers, and the realised population is published in ``detail``.
    """

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        for leg in LegName:
            matched = [
                matched_leg_ic(
                    cs.as_of_date,
                    cs.leg_values_by_security(leg),
                    cs.returns_by_security(horizon),
                )
                for cs in cross_sections
                if horizon in cs.forward_returns_usd_by_horizon and cs.leg_values_by_security(leg)
            ]
            per_date_ics = [item.value for item in matched]
            avg = average_ic(per_date_ics)
            if avg is None:
                continue
            populations = [item.population for item in matched if item.value is not None]
            candidates = [item.candidates for item in matched if item.value is not None]
            metrics.append(
                PerformanceMetric(
                    id=f"leg_ic:{cross_sections[-1].as_of_date.isoformat()}:{leg.value}:h{horizon}",
                    metric_type="leg_ic",
                    as_of_date=cross_sections[-1].as_of_date,
                    horizon_days=horizon,
                    scope=f"leg:{leg.value}",
                    value=str(avg),
                    sample_size=len(populations),
                    detail=detail_payload(
                        dates_used=len(populations),
                        matched_observations=sum(populations),
                        min_population=min(populations) if populations else 0,
                        max_population=max(populations) if populations else 0,
                        candidate_observations=sum(candidates),
                        leg_coverage_fraction=(
                            Decimal(sum(populations)) / Decimal(sum(candidates)) if sum(candidates) else None
                        ),
                    ),
                    metrics_version=DETAILED_METRICS_VERSION,
                )
            )
    return metrics


def compute_leg_correlation_metrics_per_date(
    cross_sections: list[DateCrossSection],
    *,
    metric_type: str = "leg_correlation",
) -> list[PerformanceMetric]:
    """Leg redundancy measured within each cross-section, then averaged.

    The pooled variant kept only names with all six legs populated and flattened
    every date into one vector, which both selected on completeness and let
    time-series drift masquerade as cross-sectional correlation. Here each pair
    is correlated on its own overlapping names for a single date; the reported
    value is the mean of those per-date correlations.

    ``metric_type`` defaults to the published ``leg_correlation`` type so the
    existing report surface reads an unbiased number without a schema change;
    the richer statistics ride along in ``detail``.
    """

    if not cross_sections:
        return []
    representative_by_date: dict[date, DateCrossSection] = {}
    for cross_section in cross_sections:
        current = representative_by_date.get(cross_section.as_of_date)
        if current is None or len(cross_section.security_ids) > len(
            current.security_ids
        ):
            representative_by_date[cross_section.as_of_date] = cross_section
    leg_z_by_date = {
        as_of_date: {
            leg: cross_section.leg_values_by_security(leg)
            for leg in cross_section.available_legs
        }
        for as_of_date, cross_section in representative_by_date.items()
    }
    as_of_date = cross_sections[-1].as_of_date

    metrics: list[PerformanceMetric] = []
    for (leg_a, leg_b), aggregated in sorted(
        aggregate_pair_correlations(leg_z_by_date).items(),
        key=lambda item: (item[0][0].value, item[0][1].value),
    ):
        if aggregated.mean_correlation is None:
            continue
        metrics.append(
            PerformanceMetric(
                id=f"{metric_type}:{as_of_date.isoformat()}:{leg_a.value}x{leg_b.value}",
                metric_type=metric_type,
                as_of_date=as_of_date,
                scope=f"{leg_a.value}x{leg_b.value}",
                value=str(aggregated.mean_correlation),
                sample_size=aggregated.total_observations,
                detail=detail_payload(
                    dates_used=aggregated.dates_used,
                    min_population=aggregated.min_population,
                    max_population=aggregated.max_population,
                    mean_population=(
                        Decimal(aggregated.total_observations) / Decimal(aggregated.dates_used)
                        if aggregated.dates_used
                        else None
                    ),
                    dispersion=_std_or_none(aggregated.per_date_values),
                    estimator="per_date_mean",
                ),
                metrics_version=DETAILED_METRICS_VERSION,
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


def _composite_ic_series(cross_sections: list[DateCrossSection], horizon: int) -> dict[date, Decimal]:
    series: dict[date, Decimal] = {}
    for cs in cross_sections:
        if horizon not in cs.forward_returns_usd_by_horizon:
            continue
        value = composite_ic_for_date(cs.percentiles, cs.forward_returns_usd_by_horizon[horizon])
        if value is not None:
            series[cs.as_of_date] = value
    return series


def compute_ic_distribution_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    """Shape of the per-date IC series: dispersion, quantiles, ICIR, overlap-aware t."""

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        series = _composite_ic_series(cross_sections, horizon)
        distribution = ic_distribution(list(series.values()), horizon)
        if distribution is None:
            continue
        metrics.append(
            PerformanceMetric(
                id=f"ic_distribution:{as_of_date.isoformat()}:universe:h{horizon}",
                metric_type="ic_distribution",
                as_of_date=as_of_date,
                horizon_days=horizon,
                scope="universe",
                value=str(distribution.mean),
                sample_size=distribution.count,
                detail=detail_payload(
                    std=distribution.std,
                    minimum=distribution.minimum,
                    q10=distribution.q10,
                    q25=distribution.q25,
                    median=distribution.median,
                    q75=distribution.q75,
                    q90=distribution.q90,
                    maximum=distribution.maximum,
                    positive_fraction=distribution.positive_fraction,
                    icir=distribution.icir,
                    effective_sample_size=distribution.effective_sample_size,
                    t_statistic=distribution.t_statistic,
                    p_value=distribution.p_value,
                ),
                metrics_version=DETAILED_METRICS_VERSION,
            )
        )
    return metrics


def compute_ic_interval_metrics(
    cross_sections: list[DateCrossSection],
    *,
    seed_text: str = DEFAULT_SEED_TEXT,
) -> list[PerformanceMetric]:
    """Newey-West and seeded block-bootstrap confidence intervals for the mean IC."""

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        values = list(_composite_ic_series(cross_sections, horizon).values())
        seed = seed_from_text(f"{seed_text}:ic_interval:{horizon}")
        for interval in (
            newey_west_interval(values, horizon_days=horizon),
            block_bootstrap_interval(values, horizon_days=horizon, seed=seed),
        ):
            if interval is None:
                continue
            metrics.append(
                PerformanceMetric(
                    id=f"ic_interval:{as_of_date.isoformat()}:{interval.method}:h{horizon}",
                    metric_type="ic_interval",
                    as_of_date=as_of_date,
                    horizon_days=horizon,
                    scope=interval.method,
                    value=str(interval.point),
                    sample_size=interval.sample_size,
                    detail=detail_payload(
                        low=interval.low,
                        high=interval.high,
                        confidence=interval.confidence,
                        method=interval.method,
                        standard_error=interval.standard_error,
                        effective_sample_size=interval.effective_sample_size,
                        excludes_zero=interval.excludes_zero,
                        seed=interval.seed,
                        replicates=interval.replicates,
                    ),
                    metrics_version=DETAILED_METRICS_VERSION,
                )
            )
    return metrics


def compute_spread_metrics(
    cross_sections: list[DateCrossSection],
    *,
    cost_per_unit_turnover: Decimal | None = None,
) -> list[PerformanceMetric]:
    """Top-minus-bottom spread with robustness, turnover, cost and drawdown.

    Cost-adjusted figures appear only when the caller supplies a cost per unit
    of turnover; the fee schedule is portfolio policy and is not assumed here.
    """

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date
    ordered = sorted(cross_sections, key=lambda cs: cs.as_of_date)
    session_index = {
        as_of_date: index
        for index, as_of_date in enumerate(
            sorted({item.as_of_date for item in ordered})
        )
    }

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        spreads: list[Decimal] = []
        robust_spreads: list[Decimal] = []
        turnovers: list[Decimal] = []
        outliers = 0
        populations = 0
        previous_top: tuple[str, ...] | None = None
        previous_bottom: tuple[str, ...] | None = None
        non_overlapping_spreads: list[Decimal] = []
        last_selected_session_index: int | None = None
        for cs in ordered:
            returns = cs.returns_by_security(horizon)
            if not returns:
                continue
            result = top_minus_bottom(cs.percentile_by_security, returns)
            if result is None:
                continue
            spreads.append(result.spread)
            if result.robust_spread is not None:
                robust_spreads.append(result.robust_spread)
            outliers += result.outlier_count
            populations += result.population
            current_session_index = session_index[cs.as_of_date]
            if (
                last_selected_session_index is None
                or current_session_index - last_selected_session_index
                >= horizon
            ):
                non_overlapping_spreads.append(
                    result.robust_spread
                    if result.robust_spread is not None
                    else result.spread
                )
                top_rotation = turnover(previous_top, result.top_ids)
                bottom_rotation = turnover(
                    previous_bottom,
                    result.bottom_ids,
                )
                if top_rotation is not None and bottom_rotation is not None:
                    turnovers.append(top_rotation + bottom_rotation)
                previous_top = result.top_ids
                previous_bottom = result.bottom_ids
                last_selected_session_index = current_session_index

        if not spreads:
            continue
        average_spread = sum(spreads, ZERO) / Decimal(len(spreads))
        non_overlapping_spread = mean(non_overlapping_spreads)
        average_turnover = mean(turnovers)
        metrics.append(
            PerformanceMetric(
                id=f"spread:{as_of_date.isoformat()}:top_minus_bottom:h{horizon}",
                metric_type="spread",
                as_of_date=as_of_date,
                horizon_days=horizon,
                scope="top_minus_bottom",
                value=str(average_spread),
                sample_size=len(spreads),
                detail=detail_payload(
                    robust_spread=mean(robust_spreads),
                    outlier_count=outliers,
                    matched_observations=populations,
                    mean_turnover=average_turnover,
                    turnover_observations=len(turnovers),
                    cost_per_unit_turnover=cost_per_unit_turnover,
                    cost_adjusted_spread=cost_adjusted_return(
                        (
                            non_overlapping_spread
                            if non_overlapping_spread is not None
                            else average_spread
                        ),
                        average_turnover,
                        cost_per_unit_turnover,
                    ),
                    max_drawdown=max_drawdown(non_overlapping_spreads),
                    path_sampling="non_overlapping",
                ),
                metrics_version=DETAILED_METRICS_VERSION,
            )
        )
    return metrics


def compute_benchmark_metrics(
    cross_sections: list[DateCrossSection],
    *,
    seed_text: str = DEFAULT_SEED_TEXT,
) -> list[PerformanceMetric]:
    """Equal-weight, random-ranking and momentum reference points.

    Each benchmark is emitted only where the stored data supports it; a missing
    trailing-return window simply omits the momentum comparison.
    """

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date
    ordered = sorted(cross_sections, key=lambda cs: cs.as_of_date)

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        returns_by_date = {
            cs.as_of_date: cs.returns_by_security(horizon)
            for cs in ordered
            if horizon in cs.forward_returns_usd_by_horizon
        }
        if not returns_by_date:
            continue

        equal_weight = mean(
            [
                value
                for values in returns_by_date.values()
                if (value := equal_weight_return(values)) is not None
            ]
        )
        if equal_weight is not None:
            metrics.append(
                PerformanceMetric(
                    id=f"benchmark:{as_of_date.isoformat()}:equal_weight:h{horizon}",
                    metric_type="benchmark",
                    as_of_date=as_of_date,
                    horizon_days=horizon,
                    scope="equal_weight",
                    value=str(equal_weight),
                    sample_size=len(returns_by_date),
                    detail=detail_payload(
                        dates_used=len(returns_by_date),
                        matched_observations=sum(len(values) for values in returns_by_date.values()),
                    ),
                    metrics_version=DETAILED_METRICS_VERSION,
                )
            )

        null_band = random_null_band(returns_by_date, seed=seed_from_text(f"{seed_text}:random:{horizon}"))
        composite_series = _composite_ic_series(ordered, horizon)
        composite_mean = mean(list(composite_series.values()))
        if null_band is not None:
            metrics.append(
                PerformanceMetric(
                    id=f"benchmark:{as_of_date.isoformat()}:random_ranking:h{horizon}",
                    metric_type="benchmark",
                    as_of_date=as_of_date,
                    horizon_days=horizon,
                    scope="random_ranking",
                    value=str(null_band.mean),
                    sample_size=null_band.replicates,
                    detail=detail_payload(
                        std=null_band.std,
                        p95_absolute=null_band.p95_absolute,
                        composite_mean_ic=composite_mean,
                        composite_clears_null=(
                            abs(composite_mean) > null_band.p95_absolute if composite_mean is not None else None
                        ),
                        seed=seed_from_text(f"{seed_text}:random:{horizon}"),
                    ),
                    metrics_version=DETAILED_METRICS_VERSION,
                )
            )

        momentum_series: dict[date, Decimal] = {}
        window_used = 63
        for cs in ordered:
            if window_used not in cs.trailing_returns_usd_by_window:
                continue
            value = momentum_ic(
                cs.trailing_returns_usd_by_window[window_used],
                cs.returns_by_security(horizon),
            )
            if value is not None:
                momentum_series[cs.as_of_date] = value
        momentum_mean = mean(list(momentum_series.values()))
        if momentum_mean is not None:
            metrics.append(
                PerformanceMetric(
                    id=f"benchmark:{as_of_date.isoformat()}:momentum:h{horizon}",
                    metric_type="benchmark",
                    as_of_date=as_of_date,
                    horizon_days=horizon,
                    scope="momentum",
                    value=str(momentum_mean),
                    sample_size=len(momentum_series),
                    detail=detail_payload(
                        trailing_window_sessions=window_used,
                        composite_mean_ic=composite_mean,
                        composite_minus_momentum=(
                            composite_mean - momentum_mean if composite_mean is not None else None
                        ),
                    ),
                    metrics_version=DETAILED_METRICS_VERSION,
                )
            )
    return metrics


def compute_coverage_bias_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    """How much of the measured signal tracks data availability rather than content."""

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date
    ordered = sorted(cross_sections, key=lambda cs: cs.as_of_date)

    metrics: list[PerformanceMetric] = []
    for horizon in HORIZONS:
        score_correlations: list[Decimal] = []
        return_correlations: list[Decimal] = []
        high_ics: list[Decimal] = []
        low_ics: list[Decimal] = []
        populations = 0
        dates_used = 0
        for cs in ordered:
            if not cs.coverage_by_security:
                continue
            result = coverage_bias(
                cs.coverage_by_security,
                cs.percentile_by_security,
                cs.returns_by_security(horizon),
            )
            if result is None:
                continue
            dates_used += 1
            populations += result.population
            if result.coverage_score_correlation is not None:
                score_correlations.append(result.coverage_score_correlation)
            if result.coverage_return_correlation is not None:
                return_correlations.append(result.coverage_return_correlation)
            if result.high_coverage_ic is not None:
                high_ics.append(result.high_coverage_ic)
            if result.low_coverage_ic is not None:
                low_ics.append(result.low_coverage_ic)

        mean_score_correlation = mean(score_correlations)
        if mean_score_correlation is None:
            continue
        high_mean = mean(high_ics)
        low_mean = mean(low_ics)
        metrics.append(
            PerformanceMetric(
                id=f"coverage_bias:{as_of_date.isoformat()}:universe:h{horizon}",
                metric_type="coverage_bias",
                as_of_date=as_of_date,
                horizon_days=horizon,
                scope="universe",
                value=str(mean_score_correlation),
                sample_size=dates_used,
                detail=detail_payload(
                    coverage_return_correlation=mean(return_correlations),
                    high_coverage_ic=high_mean,
                    low_coverage_ic=low_mean,
                    coverage_ic_gap=(high_mean - low_mean if high_mean is not None and low_mean is not None else None),
                    matched_observations=populations,
                ),
                metrics_version=DETAILED_METRICS_VERSION,
            )
        )
    return metrics


def compute_multiple_testing_metrics(cross_sections: list[DateCrossSection]) -> list[PerformanceMetric]:
    """Benjamini-Hochberg FDR control across the composite and per-leg IC family.

    Six legs times three horizons plus the composite is a large enough family
    that an uncorrected 5% threshold is expected to produce a false positive by
    construction; the adjusted q-value is what should be read.
    """

    if not cross_sections:
        return []
    as_of_date = cross_sections[-1].as_of_date

    p_values: dict[str, Decimal] = {}
    horizon_by_label: dict[str, int] = {}
    for horizon in HORIZONS:
        composite = ic_distribution(list(_composite_ic_series(cross_sections, horizon).values()), horizon)
        if composite is not None and composite.p_value is not None:
            label = f"composite:h{horizon}"
            p_values[label] = composite.p_value
            horizon_by_label[label] = horizon
        for leg in LegName:
            series = [
                matched_leg_ic(
                    cs.as_of_date,
                    cs.leg_values_by_security(leg),
                    cs.returns_by_security(horizon),
                ).value
                for cs in cross_sections
                if horizon in cs.forward_returns_usd_by_horizon and cs.leg_values_by_security(leg)
            ]
            distribution = ic_distribution(series, horizon)
            if distribution is None or distribution.p_value is None:
                continue
            label = f"leg:{leg.value}:h{horizon}"
            p_values[label] = distribution.p_value
            horizon_by_label[label] = horizon

    results = benjamini_hochberg(p_values)
    return [
        PerformanceMetric(
            id=f"multiple_testing:{as_of_date.isoformat()}:{result.label}",
            metric_type="multiple_testing",
            as_of_date=as_of_date,
            horizon_days=horizon_by_label[result.label],
            scope=result.label,
            value=decimal_str(result.q_value),
            sample_size=len(results),
            detail=detail_payload(
                p_value=result.p_value,
                q_value=result.q_value,
                rejected=result.rejected,
                rank=result.rank,
                family_size=len(results),
                method="benjamini_hochberg",
            ),
            metrics_version=DETAILED_METRICS_VERSION,
        )
        for result in results
    ]


def compute_detailed_metrics(
    cross_sections: list[DateCrossSection],
    *,
    seed_text: str = DEFAULT_SEED_TEXT,
    cost_per_unit_turnover: Decimal | None = None,
) -> list[PerformanceMetric]:
    """Every versioned statistic, in a stable order, for one measurement run."""

    if not cross_sections:
        return []
    return [
        *compute_ic_distribution_metrics(cross_sections),
        *compute_ic_interval_metrics(cross_sections, seed_text=seed_text),
        *compute_leg_correlation_metrics_per_date(cross_sections),
        *compute_spread_metrics(cross_sections, cost_per_unit_turnover=cost_per_unit_turnover),
        *compute_benchmark_metrics(cross_sections, seed_text=seed_text),
        *compute_coverage_bias_metrics(cross_sections),
        *compute_multiple_testing_metrics(cross_sections),
    ]
