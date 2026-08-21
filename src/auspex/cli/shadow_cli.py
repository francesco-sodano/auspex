"""Shadow-study CLI seam (arc42 §5.8).

Assembles :class:`~auspex.performance.shadow.ShadowCrossSection` inputs from the
``scores`` container and full price history, runs a pre-registered comparison,
and renders the result. Publishing is opt-in: the default is a dry run, because
a shadow study is an experiment and an experiment that writes to the shared
performance surface by default stops being one.

This module is deliberately thin. All statistics live in
:mod:`auspex.performance.shadow` and the modules beneath it, so the comparison
is testable without any repository or Cosmos wiring.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Protocol

from auspex.models.enums import FilerProfile, LegName
from auspex.models.performance import PerformanceMetric
from auspex.performance.shadow import (
    PreRegistration,
    ShadowCrossSection,
    ShadowReport,
    ShadowVariant,
    default_pre_registration,
    promotion_verdict,
    run_shadow_comparison,
    shadow_metrics,
)

logger = logging.getLogger(__name__)

#: Legs that do not apply to a foreign private issuer (arc42 §5.6): no 13F/13D
#: filings exist for it, so scoring it on smart money would penalise a structural
#: absence of evidence rather than a weak signal.
FPI_EXCLUDED_LEGS = frozenset({LegName.SMART_MONEY})


class _Snapshot(Protocol):
    security_id: str
    as_of_date: date
    percentile: str | None
    filer_profile: FilerProfile
    legs: dict


def applicable_legs_for(snapshot: _Snapshot) -> frozenset[LegName]:
    """Which legs structurally apply to one scored security."""

    if snapshot.filer_profile == FilerProfile.FPI:
        return frozenset(LegName) - FPI_EXCLUDED_LEGS
    return frozenset(LegName)


def build_cross_sections(
    snapshots: list,
    forward_return: object,
    horizons: tuple[int, ...],
) -> list[ShadowCrossSection]:
    """Group scored snapshots into per-date shadow cross-sections.

    ``forward_return`` is any callable ``(security_id, as_of, horizon) ->
    Decimal | None``; injecting it keeps this function free of price-repository
    detail and trivially testable.
    """

    by_date: dict[date, list] = {}
    for snapshot in snapshots:
        composite = getattr(snapshot, "composite", None)
        if composite is None:
            continue
        by_date.setdefault(snapshot.as_of_date, []).append(snapshot)

    cross_sections: list[ShadowCrossSection] = []
    for as_of in sorted(by_date):
        rows = sorted(by_date[as_of], key=lambda snap: snap.security_id)
        returns_by_horizon: dict[int, dict[str, Decimal]] = {}
        for horizon in horizons:
            matched = {}
            for snapshot in rows:
                value = forward_return(snapshot.security_id, as_of, horizon)  # type: ignore[operator]
                if value is not None:
                    matched[snapshot.security_id] = value
            if matched:
                returns_by_horizon[horizon] = matched
        if not returns_by_horizon:
            continue
        cross_sections.append(
            ShadowCrossSection(
                as_of_date=as_of,
                champion_scores_by_security={
                    snapshot.security_id: Decimal(snapshot.composite)
                    for snapshot in rows
                },
                leg_z_by_security={
                    snapshot.security_id: {
                        leg: Decimal(result.z)
                        for leg, result in snapshot.legs.items()
                        if result is not None and result.z is not None
                    }
                    for snapshot in rows
                },
                forward_returns_usd_by_horizon=returns_by_horizon,
                applicable_legs_by_security={
                    snapshot.security_id: applicable_legs_for(snapshot) for snapshot in rows
                },
            )
        )
    return cross_sections


def render_report(report: ShadowReport) -> list[str]:
    """A plain-text rendering suitable for a job log or an operator's terminal."""

    lines = [
        f"shadow study {report.registration.study_id}",
        f"  fingerprint      {report.fingerprint}",
        f"  registered_on    {report.registration.registered_on.isoformat()}",
        f"  primary_metric   {report.registration.primary_metric}",
        f"  dates_evaluated  {report.dates_evaluated}"
        + (f" (UNDERPOWERED, minimum {report.registration.minimum_dates})" if report.underpowered else ""),
        "  variants:",
    ]
    for result in report.results:
        if result.mean_ic is None:
            lines.append(f"    {result.variant:<20} h{result.horizon_days:<4} no eligible dates")
            continue
        icir = "n/a" if result.icir is None else f"{result.icir:.4f}"
        lines.append(
            f"    {result.variant:<20} h{result.horizon_days:<4} "
            f"mean_ic={result.mean_ic:.6f} icir={icir} dates={result.dates_used}"
        )
    lines.append("  vs champion:")
    for comparison in report.comparisons:
        if comparison.mean_difference is None:
            lines.append(f"    {comparison.variant:<20} h{comparison.horizon_days:<4} no paired dates")
            continue
        q = "n/a" if comparison.q_value is None else f"{comparison.q_value:.4f}"
        lines.append(
            f"    {comparison.variant:<20} h{comparison.horizon_days:<4} "
            f"delta={comparison.mean_difference:+.6f} q={q} "
            f"verdict={promotion_verdict(report, comparison)}"
        )
    return lines


async def run_shadow_study(
    snapshots: list,
    forward_return: object,
    *,
    registration: PreRegistration | None = None,
    challengers: tuple[ShadowVariant, ...] = (),
    as_of_date: date | None = None,
    publish: bool = False,
    performance_repo=None,
) -> tuple[ShadowReport, list[PerformanceMetric]]:
    """Run one pre-registered study, publishing only when explicitly asked.

    Returns the report and the metric rows it produced. When ``publish`` is
    false the rows are still returned — a dry run should show exactly what it
    would have written.
    """

    registration = registration or default_pre_registration(
        as_of_date or date.today(),
        challengers=challengers,
    )
    cross_sections = build_cross_sections(snapshots, forward_return, registration.horizons)
    report = run_shadow_comparison(registration, cross_sections)
    metrics = shadow_metrics(report)

    for line in render_report(report):
        logger.info("%s", line)

    if not publish:
        logger.info("shadow: dry run — %d metric row(s) computed, none written", len(metrics))
        return report, metrics

    if performance_repo is None:
        raise ValueError("publishing a shadow study requires a performance repository")
    for metric in metrics:
        await performance_repo.upsert(metric)
    logger.info("shadow: published %d metric row(s) to the performance container", len(metrics))
    return report, metrics
