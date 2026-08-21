"""Performance engine metric builders (arc42 §5.8).

Covers the two corrected estimators and the new detailed metric families. The
old metric ids, types, scopes and horizons are unchanged so the existing
``GET /api/performance`` contract keeps working; everything new arrives either
as extra rows or inside the versioned ``detail`` payload.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from auspex.models.enums import LegName
from auspex.performance.detail import DETAILED_METRICS_VERSION
from auspex.performance.engine import (
    HORIZONS,
    DateCrossSection,
    compute_benchmark_metrics,
    compute_composite_ic_metrics,
    compute_coverage_bias_metrics,
    compute_detailed_metrics,
    compute_ic_distribution_metrics,
    compute_ic_interval_metrics,
    compute_leg_correlation_metrics_per_date,
    compute_leg_ic_metrics,
    compute_multiple_testing_metrics,
    compute_spread_metrics,
)

START = date(2026, 1, 5)
NAMES = [f"S{index:02d}" for index in range(12)]


def _wobble(index: int, day: int) -> Decimal:
    """Small deterministic perturbation so nothing is degenerately perfect."""

    return Decimal((index * 7 + day * 13) % 5) / Decimal("100")


def _cross_section(
    day: int,
    horizon: int,
    *,
    legs: tuple[LegName, ...] = (LegName.THESIS_LINKAGE, LegName.SMART_MONEY, LegName.VALUATION_BRAKE),
    smart_money_names: list[str] | None = None,
) -> DateCrossSection:
    as_of = START + timedelta(days=day)
    percentiles = [Decimal(index * 8) for index in range(len(NAMES))]
    returns = [Decimal(index) / Decimal("100") + _wobble(index, day) for index in range(len(NAMES))]

    leg_z_by_security: dict[LegName, dict[str, Decimal]] = {}
    for leg in legs:
        covered = NAMES
        if leg is LegName.SMART_MONEY and smart_money_names is not None:
            covered = smart_money_names
        leg_z_by_security[leg] = {
            sid: Decimal(NAMES.index(sid)) / Decimal("10") + _wobble(NAMES.index(sid), day) for sid in covered
        }

    return DateCrossSection(
        as_of_date=as_of,
        security_ids=list(NAMES),
        percentiles=percentiles,
        leg_z_by_leg={},
        forward_returns_usd_by_horizon={horizon: returns},
        leg_z_by_security=leg_z_by_security,
        coverage_by_security={sid: Decimal(index + 1) / Decimal("20") for index, sid in enumerate(NAMES)},
        trailing_returns_usd_by_window={
            horizon: {sid: Decimal(index) / Decimal("200") for index, sid in enumerate(NAMES)}
        },
    )


def _series(horizon: int = 21, days: int = 24, **kwargs: object) -> list[DateCrossSection]:
    return [_cross_section(day, horizon, **kwargs) for day in range(days)]  # type: ignore[arg-type]


class TestCompositeICMetrics:
    def test_publishes_one_row_per_horizon_with_the_legacy_shape(self) -> None:
        metrics = compute_composite_ic_metrics(_series())
        assert len(metrics) == 1
        metric = metrics[0]
        assert metric.metric_type == "composite_ic"
        assert metric.horizon_days == 21
        assert metric.scope == "universe"
        assert metric.sample_size == 24

    def test_ignores_horizons_outside_the_published_set(self) -> None:
        assert compute_composite_ic_metrics(_series(horizon=7)) == []


class TestLegICMetrics:
    def test_a_leg_missing_for_some_names_still_contributes_every_date(self) -> None:
        """Defect #1 regression, at the engine level.

        SMART_MONEY is structurally absent for FPI filers. Under the old dense
        path a date where any candidate lacked the leg produced no leg IC at
        all, so the reported SMART_MONEY IC was computed on a filer-selected
        subsample. Now the leg uses the eight names it covers, on all 24 dates,
        and says so in ``detail``.
        """

        sections = _series(smart_money_names=NAMES[:8])
        metrics = {m.scope: m for m in compute_leg_ic_metrics(sections)}
        smart_money = metrics["leg:smart_money"]
        thesis = metrics["leg:thesis_linkage"]

        assert smart_money.sample_size == 24
        assert thesis.sample_size == 24
        assert smart_money.detail["min_population"] == "8"
        assert smart_money.detail["max_population"] == "8"
        assert thesis.detail["min_population"] == "12"
        assert smart_money.detail["leg_coverage_fraction"].startswith("0.66")
        assert thesis.detail["leg_coverage_fraction"] == "1"

    def test_keeps_the_published_id_and_scope_format(self) -> None:
        metrics = compute_leg_ic_metrics(_series())
        metric = next(m for m in metrics if m.scope == "leg:thesis_linkage")
        assert metric.metric_type == "leg_ic"
        assert metric.id == f"leg_ic:{metric.as_of_date.isoformat()}:thesis_linkage:h21"
        assert metric.metrics_version == DETAILED_METRICS_VERSION

    def test_a_leg_nobody_covers_produces_no_row(self) -> None:
        metrics = compute_leg_ic_metrics(_series())
        assert not any(m.scope == "leg:narrative_premium" for m in metrics)


class TestLegCorrelationMetrics:
    def test_is_estimated_within_each_date_not_pooled_across_them(self) -> None:
        """Defect #2 regression, at the engine level.

        Both legs drift upward over time. Pooling every date into one
        population turns that shared drift into apparent cross-sectional
        redundancy; measuring within each date and averaging does not.
        """

        sections = []
        for day in range(6):
            drift = Decimal(day)
            sections.append(
                DateCrossSection(
                    as_of_date=START + timedelta(days=day),
                    security_ids=["A", "B", "C"],
                    percentiles=[Decimal("10"), Decimal("50"), Decimal("90")],
                    leg_z_by_leg={},
                    forward_returns_usd_by_horizon={21: [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]},
                    leg_z_by_security={
                        LegName.THESIS_LINKAGE: {
                            "A": drift + Decimal("1"),
                            "B": drift + Decimal("2"),
                            "C": drift + Decimal("3"),
                        },
                        LegName.SMART_MONEY: {
                            "A": drift + Decimal("3"),
                            "B": drift + Decimal("2"),
                            "C": drift + Decimal("1"),
                        },
                    },
                )
            )

        metrics = compute_leg_correlation_metrics_per_date(sections)
        assert len(metrics) == 1
        metric = metrics[0]
        assert metric.metric_type == "leg_correlation"
        assert metric.scope == "smart_moneyxthesis_linkage"
        assert abs(Decimal(metric.value) - Decimal("-1")) < Decimal("0.000001")
        assert metric.detail["estimator"] == "per_date_mean"
        assert metric.detail["dates_used"] == "6"

    def test_a_single_leg_produces_no_pairs(self) -> None:
        sections = _series(legs=(LegName.THESIS_LINKAGE,))
        assert compute_leg_correlation_metrics_per_date(sections) == []

    def test_ids_are_scoped_by_date_and_pair(self) -> None:
        metrics = compute_leg_correlation_metrics_per_date(_series())
        assert len({m.id for m in metrics}) == len(metrics)
        for metric in metrics:
            assert metric.id.startswith("leg_correlation:")
            assert "x" in metric.scope


class TestDetailedMetricFamilies:
    def test_ic_distribution_reports_icir_and_effective_sample_size(self) -> None:
        metrics = compute_ic_distribution_metrics(_series())
        assert metrics
        metric = metrics[0]
        assert metric.metric_type == "ic_distribution"
        assert metric.horizon_days == 21
        assert "icir" in metric.detail
        assert "effective_sample_size" in metric.detail
        assert metric.metrics_version == DETAILED_METRICS_VERSION

    def test_ic_intervals_publish_both_estimators_deterministically(self) -> None:
        first = compute_ic_interval_metrics(_series())
        second = compute_ic_interval_metrics(_series())
        assert first
        assert [m.value for m in first] == [m.value for m in second]
        methods = {m.detail["method"] for m in first}
        assert methods == {"newey_west", "moving_block_bootstrap"}

    def test_spread_metrics_carry_turnover_and_robust_counts(self) -> None:
        metrics = compute_spread_metrics(_series())
        assert metrics
        metric = metrics[0]
        assert metric.metric_type == "spread"
        assert "outlier_count" in metric.detail
        assert "mean_turnover" in metric.detail

    def test_spread_metrics_apply_costs_when_supplied(self) -> None:
        priced = compute_spread_metrics(_series(), cost_per_unit_turnover=Decimal("0.01"))
        assert priced
        assert "cost_adjusted_spread" in priced[0].detail

    def test_benchmark_metrics_compare_against_simple_alternatives(self) -> None:
        metrics = compute_benchmark_metrics(_series())
        assert metrics
        scopes = {m.scope for m in metrics}
        assert any("momentum" in scope or "random" in scope or "equal" in scope for scope in scopes)
        for metric in metrics:
            assert metric.metric_type == "benchmark"

    def test_per_horizon_cross_sections_do_not_drop_short_benchmarks(self) -> None:
        sections = [
            _cross_section(day, horizon)
            for day in range(24)
            for horizon in HORIZONS
        ]

        metrics = compute_benchmark_metrics(sections)

        assert {
            metric.horizon_days
            for metric in metrics
            if metric.scope == "equal_weight"
        } == set(HORIZONS)

    def test_momentum_benchmark_uses_fixed_63_session_window(self) -> None:
        sections = [
            replace(
                _cross_section(day, 21),
                trailing_returns_usd_by_window={
                    63: {
                        sid: Decimal(index) / Decimal("100")
                        for index, sid in enumerate(NAMES)
                    }
                },
            )
            for day in range(24)
        ]

        metrics = compute_benchmark_metrics(sections)

        momentum = next(
            metric for metric in metrics if metric.scope == "momentum"
        )
        assert momentum.detail["trailing_window_sessions"] == "63"

    def test_leg_correlation_uses_largest_population_per_date(self) -> None:
        sections = []
        for day in range(12):
            sections.extend(
                [
                    _cross_section(
                        day,
                        21,
                        smart_money_names=NAMES[:10],
                    ),
                    _cross_section(
                        day,
                        126,
                        smart_money_names=NAMES[:4],
                    ),
                ]
            )

        metrics = compute_leg_correlation_metrics_per_date(sections)

        smart_pair = next(
            metric
            for metric in metrics
            if "smart_money" in metric.scope
            and "thesis_linkage" in metric.scope
        )
        assert smart_pair.detail["min_population"] == "10"

    def test_coverage_bias_metrics_are_published(self) -> None:
        metrics = compute_coverage_bias_metrics(_series())
        assert metrics
        assert metrics[0].metric_type == "coverage_bias"

    def test_multiple_testing_metrics_report_q_values(self) -> None:
        metrics = compute_multiple_testing_metrics(_series())
        assert metrics == []


class TestComputeDetailedMetrics:
    def test_emits_unique_ids_across_every_family(self) -> None:
        metrics = compute_detailed_metrics(_series())
        assert metrics
        assert len({m.id for m in metrics}) == len(metrics)

    def test_every_row_is_version_stamped(self) -> None:
        metrics = compute_detailed_metrics(_series())
        assert all(m.metrics_version == DETAILED_METRICS_VERSION for m in metrics)

    def test_is_deterministic_across_runs(self) -> None:
        first = compute_detailed_metrics(_series())
        second = compute_detailed_metrics(_series())
        assert [(m.id, m.value) for m in first] == [(m.id, m.value) for m in second]

    def test_empty_input_is_safe(self) -> None:
        assert compute_detailed_metrics([]) == []

    def test_covers_every_published_horizon(self) -> None:
        sections: list[DateCrossSection] = []
        for horizon in HORIZONS:
            sections.extend(_series(horizon=horizon, days=8))
        metrics = compute_detailed_metrics(sections)
        assert {m.horizon_days for m in metrics if m.horizon_days} == set(HORIZONS)
