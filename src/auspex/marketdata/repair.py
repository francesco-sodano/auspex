"""Idempotent repair planning for the derived adjusted price series (arc42 §5.3).

Planning is pure: :func:`plan_security_repair` takes the bars of one security
and returns the rewritten bars plus an auditable
:class:`~auspex.models.market_integrity.SecurityIntegrityReport`. Nothing is
written here — :mod:`auspex.marketdata.service` owns persistence.

Two invariants hold for every plan:

* **Raw observations are immutable.** Only ``close_adjusted`` and
  ``adjustment_factor`` are ever rewritten, and the provider's original values
  are captured once into ``close_adjusted_source`` / ``adjustment_factor_source``
  before the first rewrite.
* **A split is never invented.** When a raw scale break has no authoritative
  ``split_factor``/``dividend_amount`` behind it the affected history is
  quarantined instead of being back-adjusted by a guessed ratio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from auspex.marketdata.detect import (
    CONVENTION_TOTAL_RETURN,
    detect_adjusted_inconsistency,
    detect_convention,
    detect_forward_return_anomalies,
    detect_series_anomalies,
    evaluate_bar,
    expected_factors,
    is_structurally_sound,
    sort_bars,
    to_decimal,
)
from auspex.marketdata.policy import DEFAULT_POLICY, IntegrityPolicy
from auspex.models.market import PriceBar
from auspex.models.market_integrity import (
    AffectedRange,
    BarFieldRepair,
    IntegrityCode,
    IntegrityFinding,
    IntegritySeverity,
    SecurityIntegrityReport,
)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class SecurityRepairPlan:
    """Bars to upsert plus the report explaining every change."""

    security_id: str
    report: SecurityIntegrityReport
    updates: list[PriceBar] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.updates)


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return value


def _relative_deviation(actual: Decimal, expected: Decimal) -> Decimal | None:
    if expected == _ZERO:
        return None
    try:
        return abs((actual - expected) / expected)
    except (InvalidOperation, ZeroDivisionError):
        return None


def _span(security_id: str, dates: Iterable[date], reason: str) -> list[AffectedRange]:
    ordered = sorted(set(dates))
    if not ordered:
        return []
    return [
        AffectedRange(
            security_id=security_id,
            start_date=ordered[0],
            end_date=ordered[-1],
            reason=reason,
        )
    ]


def _duplicate_groups(bars: list[PriceBar]) -> dict[date, list[PriceBar]]:
    groups: dict[date, list[PriceBar]] = {}
    for bar in sort_bars(bars):
        groups.setdefault(bar.session_date, []).append(bar)
    return groups


def plan_security_repair(
    security_id: str,
    bars: list[PriceBar],
    *,
    revision: int,
    now: datetime,
    policy: IntegrityPolicy = DEFAULT_POLICY,
    ticker: str | None = None,
) -> SecurityRepairPlan:
    """Diagnose one security and produce the minimal set of bar rewrites."""

    ordered = sort_bars(bars)
    findings: list[IntegrityFinding] = []
    quarantine_codes: dict[str, set[str]] = {}

    def quarantine(bar_id: str, code: IntegrityCode) -> None:
        quarantine_codes.setdefault(bar_id, set()).add(str(code))

    # --- duplicates: keep one bar per session, quarantine the losers ---------
    groups = _duplicate_groups(ordered)
    kept: list[PriceBar] = []
    for session_date, group in sorted(groups.items()):
        winner = group[-1]
        kept.append(winner)
        for loser in group[:-1]:
            quarantine(loser.id, IntegrityCode.DUPLICATE_BAR)
            findings.append(
                IntegrityFinding(
                    security_id=security_id,
                    session_date=session_date,
                    code=IntegrityCode.DUPLICATE_BAR,
                    severity=IntegritySeverity.ERROR,
                    detail=f"duplicate bar for session; retained {winner.id!r}",
                    observed=loser.id,
                    expected=winner.id,
                )
            )

    # --- single-bar structural checks ---------------------------------------
    for bar in kept:
        bar_findings = evaluate_bar(bar, policy)
        findings.extend(bar_findings)
        for finding in bar_findings:
            if finding.severity is IntegritySeverity.ERROR:
                quarantine(bar.id, finding.code)

    sound = [bar for bar in kept if is_structurally_sound(bar)]
    by_date = {bar.session_date: bar for bar in sound}

    # --- series-level checks -------------------------------------------------
    series_findings = detect_series_anomalies(security_id, sound, policy)
    findings.extend(series_findings)
    scale_break_dates: list[date] = []
    for finding in series_findings:
        if finding.session_date is None:
            continue
        target = by_date.get(finding.session_date)
        if target is None:
            continue
        if finding.severity is IntegritySeverity.ERROR:
            quarantine(target.id, finding.code)
        if finding.code is IntegrityCode.UNEXPLAINED_SCALE_BREAK:
            scale_break_dates.append(finding.session_date)

    # A raw scale break with no authoritative event means everything before it
    # is quoted on a stale price scale. We never guess the ratio; we quarantine.
    if scale_break_dates and policy.quarantine_history_before_scale_break:
        boundary = max(scale_break_dates)
        for bar in sound:
            if bar.session_date < boundary:
                quarantine(bar.id, IntegrityCode.STALE_PRICE_SCALE)

    forward_findings = detect_forward_return_anomalies(security_id, sound, policy)
    findings.extend(forward_findings)
    for finding in forward_findings:
        if finding.severity is not IntegritySeverity.ERROR or finding.session_date is None:
            continue
        target = by_date.get(finding.session_date)
        if target is not None:
            quarantine(target.id, finding.code)

    # --- adjusted-series reconstruction -------------------------------------
    convention, convention_deviation = detect_convention(sound, policy)
    convention_confident = (
        convention_deviation is not None
        and convention_deviation <= policy.convention_tolerance
    )
    factors = (
        expected_factors(
            sound,
            include_dividends=convention == CONVENTION_TOTAL_RETURN,
        )
        if convention_confident
        else {}
    )
    if convention_confident:
        findings.extend(
            detect_adjusted_inconsistency(
                security_id,
                sound,
                factors,
                policy,
            )
        )
    else:
        findings.append(
            IntegrityFinding(
                security_id=security_id,
                code=IntegrityCode.ADJUSTED_SERIES_INCONSISTENT,
                severity=IntegritySeverity.WARNING,
                detail=(
                    "provider adjustment convention could not be verified; "
                    "derived adjusted fields were left unchanged"
                ),
                observed=(
                    str(convention_deviation)
                    if convention_deviation is not None
                    else None
                ),
                expected=(
                    f"median relative deviation <= "
                    f"{policy.convention_tolerance}"
                ),
            )
        )

    repairs: list[BarFieldRepair] = []
    updates: list[PriceBar] = []
    quarantined_dates: list[date] = []
    released_dates: list[date] = []
    sound_ids = {candidate.id for candidate in sound}

    for bar in ordered:
        changes: dict[str, object] = {}
        codes = sorted(quarantine_codes.get(bar.id, set()))

        if bar.id in sound_ids:
            close_raw = to_decimal(bar.close_raw)
            factor = factors.get(bar.session_date)
            if close_raw is not None and close_raw > _ZERO and factor is not None:
                target_adjusted = _quantize(close_raw * factor, policy.adjusted_quantum)
                target_factor = _quantize(factor, policy.factor_quantum)
                stored_adjusted = to_decimal(bar.close_adjusted)
                stored_factor = to_decimal(bar.adjustment_factor)

                adjusted_off = stored_adjusted is None or (
                    (deviation := _relative_deviation(stored_adjusted, target_adjusted)) is not None
                    and deviation > policy.adjusted_tolerance
                )
                factor_off = stored_factor is None or (
                    (deviation := _relative_deviation(stored_factor, target_factor)) is not None
                    and deviation > policy.factor_tolerance
                )

                if adjusted_off and str(target_adjusted) != bar.close_adjusted:
                    changes["close_adjusted"] = str(target_adjusted)
                    if bar.close_adjusted_source is None:
                        changes["close_adjusted_source"] = bar.close_adjusted
                    repairs.append(
                        BarFieldRepair(
                            security_id=security_id,
                            session_date=bar.session_date,
                            field_name="close_adjusted",
                            previous=bar.close_adjusted,
                            repaired=str(target_adjusted),
                        )
                    )
                if factor_off and str(target_factor) != bar.adjustment_factor:
                    changes["adjustment_factor"] = str(target_factor)
                    if bar.adjustment_factor_source is None:
                        changes["adjustment_factor_source"] = bar.adjustment_factor
                    repairs.append(
                        BarFieldRepair(
                            security_id=security_id,
                            session_date=bar.session_date,
                            field_name="adjustment_factor",
                            previous=bar.adjustment_factor,
                            repaired=str(target_factor),
                        )
                    )

        should_quarantine = bool(codes)
        if should_quarantine:
            if not bar.quarantined or sorted(bar.quarantine_codes) != codes:
                changes["quarantined"] = True
                changes["quarantine_codes"] = codes
                quarantined_dates.append(bar.session_date)
        elif bar.quarantined or bar.quarantine_codes:
            changes["quarantined"] = False
            changes["quarantine_codes"] = []
            released_dates.append(bar.session_date)

        if changes:
            changes["integrity_revision"] = revision
            changes["repaired_at"] = now
            updates.append(bar.model_copy(update=changes))

    affected: list[AffectedRange] = []
    affected.extend(_span(security_id, (repair.session_date for repair in repairs), "adjusted_repair"))
    affected.extend(_span(security_id, quarantined_dates, "quarantine"))
    affected.extend(_span(security_id, released_dates, "release"))

    report = SecurityIntegrityReport(
        security_id=security_id,
        ticker=ticker,
        bars_examined=len(ordered),
        convention=convention,
        findings=findings,
        repairs=repairs,
        quarantined_dates=sorted(set(quarantined_dates)),
        released_dates=sorted(set(released_dates)),
        affected_ranges=affected,
    )
    return SecurityRepairPlan(security_id=security_id, report=report, updates=updates)
