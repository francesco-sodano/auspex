"""Pure market-data integrity detectors (arc42 §5.3).

Every function here is side-effect free and Decimal-safe: it takes
:class:`auspex.models.market.PriceBar` rows and returns
:class:`auspex.models.market_integrity.IntegrityFinding` values. Nothing in
this module reads or writes storage, so detection is exhaustively testable in
isolation and identical whether it runs over Cosmos rows or a fixture list.

Corporate-action semantics follow the price providers
(:mod:`auspex.providers.tiingo`, :mod:`auspex.providers.alpha_vantage`):
``split_factor`` and ``dividend_amount`` are the *authoritative* per-session
event fields, ``close_adjusted``/``adjustment_factor`` are *derived*. A raw
close-to-close move that looks like a split but has no authoritative event
behind it is never turned into a synthetic split — it is reported as
:attr:`~auspex.models.market_integrity.IntegrityCode.UNEXPLAINED_SCALE_BREAK`
and quarantined.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, DivisionByZero, InvalidOperation

from auspex.marketdata.policy import DEFAULT_POLICY, IntegrityPolicy
from auspex.models.market import PriceBar
from auspex.models.market_integrity import (
    IntegrityCode,
    IntegrityFinding,
    IntegritySeverity,
)

CONVENTION_TOTAL_RETURN = "total_return"
CONVENTION_SPLIT_ONLY = "split_only"

_ONE = Decimal("1")
_ZERO = Decimal("0")


def to_decimal(value: str | Decimal | int | None) -> Decimal | None:
    """Parse a stored Decimal-as-string, returning ``None`` when unusable."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _relative_deviation(actual: Decimal, expected: Decimal) -> Decimal | None:
    if expected == _ZERO:
        return None
    try:
        return abs((actual - expected) / expected)
    except (InvalidOperation, DivisionByZero):
        return None


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def sort_bars(bars: list[PriceBar]) -> list[PriceBar]:
    return sorted(bars, key=lambda bar: (bar.session_date, bar.id))


def dedupe_bars(bars: list[PriceBar]) -> tuple[list[PriceBar], list[IntegrityFinding]]:
    """Collapse same-session rows, reporting each collision.

    ``market_daily`` ids are ``{security_id}:{session_date}`` so collisions are
    impossible for correctly-built rows; duplicates therefore indicate a
    provider batch anomaly or a legacy id scheme and are always reported.
    """

    findings: list[IntegrityFinding] = []
    unique: dict[date, PriceBar] = {}
    for bar in sort_bars(bars):
        existing = unique.get(bar.session_date)
        if existing is None:
            unique[bar.session_date] = bar
            continue
        findings.append(
            IntegrityFinding(
                security_id=bar.security_id,
                session_date=bar.session_date,
                code=IntegrityCode.DUPLICATE_BAR,
                severity=IntegritySeverity.ERROR,
                detail=f"duplicate bar for session; ids {existing.id!r} and {bar.id!r}",
                observed=bar.id,
                expected=existing.id,
            )
        )
        # Keep the later id deterministically so repeated runs agree.
        unique[bar.session_date] = bar
    return [unique[key] for key in sorted(unique)], findings


def evaluate_bar(bar: PriceBar, policy: IntegrityPolicy = DEFAULT_POLICY) -> list[IntegrityFinding]:
    """Structural checks that need only the bar itself.

    Cheap enough to run on every collector write, which keeps impossible bars
    out of scoring reads before any repair pass runs.
    """

    del policy  # single-bar checks are threshold-free
    findings: list[IntegrityFinding] = []

    def add(
        code: IntegrityCode,
        detail: str,
        observed: str | None = None,
        severity: IntegritySeverity = IntegritySeverity.ERROR,
    ) -> None:
        findings.append(
            IntegrityFinding(
                security_id=bar.security_id,
                session_date=bar.session_date,
                code=code,
                severity=severity,
                detail=detail,
                observed=observed,
            )
        )

    prices = {
        "open_raw": to_decimal(bar.open_raw),
        "high_raw": to_decimal(bar.high_raw),
        "low_raw": to_decimal(bar.low_raw),
        "close_raw": to_decimal(bar.close_raw),
    }
    for name, value in prices.items():
        if value is None:
            add(IntegrityCode.NON_POSITIVE_PRICE, f"{name} is not a finite number", str(getattr(bar, name)))
        elif value <= _ZERO:
            add(IntegrityCode.NON_POSITIVE_PRICE, f"{name} must be > 0", str(value))

    if all(value is not None and value > _ZERO for value in prices.values()):
        open_, high, low, close = (
            prices["open_raw"],
            prices["high_raw"],
            prices["low_raw"],
            prices["close_raw"],
        )
        assert open_ is not None and high is not None and low is not None and close is not None
        if high < low:
            add(IntegrityCode.IMPOSSIBLE_OHLC, "high < low", f"high={high} low={low}")
        if high < max(open_, close):
            add(
                IntegrityCode.IMPOSSIBLE_OHLC,
                "high < max(open, close)",
                f"high={high} open={open_} close={close}",
            )
        if low > min(open_, close):
            add(
                IntegrityCode.IMPOSSIBLE_OHLC,
                "low > min(open, close)",
                f"low={low} open={open_} close={close}",
            )

    if bar.volume < 0:
        add(IntegrityCode.IMPOSSIBLE_VOLUME, "volume must be >= 0", str(bar.volume))

    close_adjusted = to_decimal(bar.close_adjusted)
    if close_adjusted is None:
        add(IntegrityCode.IMPOSSIBLE_ADJUSTED, "close_adjusted is not a finite number", bar.close_adjusted)
    elif close_adjusted <= _ZERO:
        add(IntegrityCode.IMPOSSIBLE_ADJUSTED, "close_adjusted must be > 0", str(close_adjusted))

    factor = to_decimal(bar.adjustment_factor)
    if factor is None:
        add(IntegrityCode.IMPOSSIBLE_ADJUSTED, "adjustment_factor is not a finite number", bar.adjustment_factor)
    elif factor <= _ZERO:
        add(IntegrityCode.IMPOSSIBLE_ADJUSTED, "adjustment_factor must be > 0", str(factor))

    split_factor = to_decimal(bar.split_factor)
    if split_factor is None:
        add(IntegrityCode.IMPOSSIBLE_SPLIT_FACTOR, "split_factor is not a finite number", bar.split_factor)
    elif split_factor <= _ZERO:
        add(IntegrityCode.IMPOSSIBLE_SPLIT_FACTOR, "split_factor must be > 0", str(split_factor))

    dividend = to_decimal(bar.dividend_amount)
    if dividend is None:
        add(IntegrityCode.IMPOSSIBLE_DIVIDEND, "dividend_amount is not a finite number", bar.dividend_amount)
    elif dividend < _ZERO:
        add(IntegrityCode.IMPOSSIBLE_DIVIDEND, "dividend_amount must be >= 0", str(dividend))

    return findings


def is_structurally_sound(bar: PriceBar) -> bool:
    """True when the bar's own fields are usable as repair inputs."""

    return not any(
        finding.severity is IntegritySeverity.ERROR for finding in evaluate_bar(bar)
    )


def expected_factors(bars: list[PriceBar], *, include_dividends: bool) -> dict[date, Decimal]:
    """Cumulative back-adjustment factors from authoritative events only.

    Anchored so the most recent session's factor is ``1`` (matching both
    providers, whose adjusted close equals the raw close on the latest bar
    because no corporate action follows it). Walking backwards, each session's
    event scales every earlier session:

    ``factor[t-1] = factor[t] * (1 / split_factor[t]) * (close_raw[t-1] - dividend[t]) / close_raw[t-1]``
    """

    ordered = sort_bars(bars)
    factors: dict[date, Decimal] = {}
    cumulative = _ONE
    for index in range(len(ordered) - 1, -1, -1):
        bar = ordered[index]
        factors[bar.session_date] = cumulative
        if index == 0:
            continue
        previous_close = to_decimal(ordered[index - 1].close_raw)
        split_factor = to_decimal(bar.split_factor) or _ONE
        dividend = to_decimal(bar.dividend_amount) or _ZERO
        if previous_close is None or previous_close <= _ZERO or split_factor <= _ZERO:
            continue
        event = _ONE / split_factor
        if include_dividends and dividend > _ZERO:
            net = previous_close - dividend
            if net > _ZERO:
                event *= net / previous_close
        cumulative = cumulative * event
    return factors


def detect_convention(
    bars: list[PriceBar], policy: IntegrityPolicy = DEFAULT_POLICY
) -> tuple[str, Decimal | None]:
    """Infer whether the stored adjusted series is total-return or split-only.

    Uses the *median* deviation so a handful of broken bars cannot flip the
    convention; falls back to the policy default when neither candidate fits,
    which is exactly the fully-corrupt-series case.
    """

    candidates = {
        CONVENTION_TOTAL_RETURN: expected_factors(bars, include_dividends=True),
        CONVENTION_SPLIT_ONLY: expected_factors(bars, include_dividends=False),
    }
    scores: dict[str, Decimal] = {}
    for name, factors in candidates.items():
        deviations: list[Decimal] = []
        for bar in bars:
            close_raw = to_decimal(bar.close_raw)
            close_adjusted = to_decimal(bar.close_adjusted)
            factor = factors.get(bar.session_date)
            if close_raw is None or close_adjusted is None or factor is None or close_raw <= _ZERO:
                continue
            deviation = _relative_deviation(close_adjusted, close_raw * factor)
            if deviation is not None:
                deviations.append(deviation)
        median = _median(deviations)
        if median is not None:
            scores[name] = median

    default = CONVENTION_TOTAL_RETURN if policy.include_dividends_default else CONVENTION_SPLIT_ONLY
    if not scores:
        return default, None
    best = min(scores, key=lambda name: scores[name])
    if scores[best] > policy.convention_tolerance:
        return default, scores.get(default)
    return best, scores[best]


def looks_like_round_split(
    previous_close: Decimal, close: Decimal, policy: IntegrityPolicy
) -> Decimal | None:
    """Return the matched round ratio when a raw move resembles a (reverse) split."""

    if previous_close <= _ZERO or close <= _ZERO:
        return None
    ratio = previous_close / close
    if ratio < _ONE:
        ratio = _ONE / ratio
    if ratio < policy.min_split_ratio:
        return None
    for candidate in policy.candidate_split_ratios:
        deviation = _relative_deviation(ratio, candidate)
        if deviation is not None and deviation <= policy.split_ratio_tolerance:
            return candidate
    return None


def detect_series_anomalies(
    security_id: str, bars: list[PriceBar], policy: IntegrityPolicy = DEFAULT_POLICY
) -> list[IntegrityFinding]:
    """Session-to-session checks: jumps, scale breaks, split discontinuities."""

    findings: list[IntegrityFinding] = []
    ordered = [bar for bar in sort_bars(bars) if is_structurally_sound(bar)]
    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        current = ordered[index]
        previous_close = to_decimal(previous.close_raw)
        close = to_decimal(current.close_raw)
        if previous_close is None or close is None or previous_close <= _ZERO:
            continue
        split_factor = to_decimal(current.split_factor) or _ONE
        dividend = to_decimal(current.dividend_amount) or _ZERO
        explained = (close * split_factor + dividend) / previous_close - _ONE
        magnitude = abs(explained)
        observed = (
            f"close {previous_close} -> {close}, split_factor={split_factor}, "
            f"dividend={dividend}, event-adjusted return={explained}"
        )
        if split_factor != _ONE and magnitude > policy.max_abs_daily_return:
            findings.append(
                IntegrityFinding(
                    security_id=security_id,
                    session_date=current.session_date,
                    code=IntegrityCode.SPLIT_FACTOR_DISCONTINUITY,
                    severity=IntegritySeverity.ERROR,
                    detail=(
                        "recorded split_factor does not reconcile the raw close move; "
                        "raw series may already be split-adjusted"
                    ),
                    observed=observed,
                    expected=f"|event-adjusted return| <= {policy.max_abs_daily_return}",
                )
            )
            continue
        if magnitude > policy.extreme_abs_daily_return:
            findings.append(
                IntegrityFinding(
                    security_id=security_id,
                    session_date=current.session_date,
                    code=IntegrityCode.IMPLAUSIBLE_JUMP,
                    severity=IntegritySeverity.ERROR,
                    detail="single-session move exceeds the unusable-data threshold",
                    observed=observed,
                    expected=f"|return| <= {policy.extreme_abs_daily_return}",
                )
            )
            continue
        if magnitude > policy.max_abs_daily_return:
            matched = (
                looks_like_round_split(previous_close, close, policy)
                if split_factor == _ONE and dividend == _ZERO
                else None
            )
            if matched is not None:
                findings.append(
                    IntegrityFinding(
                        security_id=security_id,
                        session_date=current.session_date,
                        code=IntegrityCode.UNEXPLAINED_SCALE_BREAK,
                        severity=IntegritySeverity.WARNING,
                        detail=(
                            f"raw close moves by ~{matched}x with no authoritative split or "
                            "dividend; no split is inferred and no history is quarantined "
                            "without corroborating corporate-action evidence"
                        ),
                        observed=observed,
                        expected="split_factor != 1 or dividend_amount > 0",
                    )
                )
            else:
                findings.append(
                    IntegrityFinding(
                        security_id=security_id,
                        session_date=current.session_date,
                        code=IntegrityCode.IMPLAUSIBLE_JUMP,
                        severity=IntegritySeverity.WARNING,
                        detail="single-session move exceeds the suspicious-move threshold",
                        observed=observed,
                        expected=f"|return| <= {policy.max_abs_daily_return}",
                    )
                )
    return findings


def detect_adjusted_inconsistency(
    security_id: str,
    bars: list[PriceBar],
    factors: dict[date, Decimal],
    policy: IntegrityPolicy = DEFAULT_POLICY,
) -> list[IntegrityFinding]:
    """Compare the stored adjusted series with the authoritative reconstruction."""

    findings: list[IntegrityFinding] = []
    for bar in sort_bars(bars):
        close_raw = to_decimal(bar.close_raw)
        close_adjusted = to_decimal(bar.close_adjusted)
        factor = factors.get(bar.session_date)
        if close_raw is None or close_raw <= _ZERO or close_adjusted is None or factor is None:
            continue
        expected_adjusted = close_raw * factor
        deviation = _relative_deviation(close_adjusted, expected_adjusted)
        if deviation is not None and deviation > policy.adjusted_tolerance:
            findings.append(
                IntegrityFinding(
                    security_id=security_id,
                    session_date=bar.session_date,
                    code=IntegrityCode.ADJUSTED_SERIES_INCONSISTENT,
                    severity=IntegritySeverity.WARNING,
                    detail="close_adjusted disagrees with the split/dividend reconstruction",
                    observed=str(close_adjusted),
                    expected=str(expected_adjusted),
                )
            )
            continue
        stored_factor = to_decimal(bar.adjustment_factor)
        implied = close_adjusted / close_raw
        if stored_factor is not None and stored_factor > _ZERO:
            factor_deviation = _relative_deviation(stored_factor, implied)
            if factor_deviation is not None and factor_deviation > policy.factor_tolerance:
                findings.append(
                    IntegrityFinding(
                        security_id=security_id,
                        session_date=bar.session_date,
                        code=IntegrityCode.ADJUSTED_FACTOR_MISMATCH,
                        severity=IntegritySeverity.WARNING,
                        detail="adjustment_factor disagrees with close_adjusted / close_raw",
                        observed=str(stored_factor),
                        expected=str(implied),
                    )
                )
    return findings


def detect_forward_return_anomalies(
    security_id: str, bars: list[PriceBar], policy: IntegrityPolicy = DEFAULT_POLICY
) -> list[IntegrityFinding]:
    """Flag horizons whose forward return on the adjusted series is impossible.

    The finding is attributed to the worst single session inside the window so
    quarantine lands on the culprit bar rather than the window start. When no
    single session inside the window is itself implausible the anomaly is a
    warning only: an extreme-but-real compounding run must not silently delete
    a security's history.
    """

    findings: list[IntegrityFinding] = []
    seen: set[tuple[int, date]] = set()
    ordered = [bar for bar in sort_bars(bars) if is_structurally_sound(bar)]
    adjusted = [to_decimal(bar.close_adjusted) for bar in ordered]
    for horizon in policy.forward_return_horizons:
        if horizon <= 0:
            continue
        for index in range(len(ordered) - horizon):
            start = adjusted[index]
            end = adjusted[index + horizon]
            if start is None or end is None or start <= _ZERO:
                continue
            forward_return = end / start - _ONE
            if abs(forward_return) <= policy.max_abs_forward_return:
                continue
            culprit_index = index + 1
            worst = _ZERO
            for step in range(index + 1, index + horizon + 1):
                previous = adjusted[step - 1]
                current = adjusted[step]
                if previous is None or current is None or previous <= _ZERO:
                    continue
                move = abs(current / previous - _ONE)
                if move > worst:
                    worst = move
                    culprit_index = step
            severity = (
                IntegritySeverity.ERROR
                if worst > policy.max_abs_daily_return
                else IntegritySeverity.WARNING
            )
            key = (horizon, ordered[culprit_index].session_date)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                IntegrityFinding(
                    security_id=security_id,
                    session_date=ordered[culprit_index].session_date,
                    code=IntegrityCode.FORWARD_RETURN_ANOMALY,
                    severity=severity,
                    detail=(
                        f"{horizon}-session forward return from "
                        f"{ordered[index].session_date.isoformat()} is implausible"
                    ),
                    observed=str(forward_return),
                    expected=f"|forward return| <= {policy.max_abs_forward_return}",
                )
            )
    return findings


def diagnose_security(
    security_id: str, bars: list[PriceBar], policy: IntegrityPolicy = DEFAULT_POLICY
) -> tuple[list[IntegrityFinding], str]:
    """Full diagnosis for one security. Returns findings and the detected convention."""

    unique, findings = dedupe_bars(bars)
    for bar in unique:
        findings.extend(evaluate_bar(bar, policy))
    sound = [bar for bar in unique if is_structurally_sound(bar)]
    convention, _ = detect_convention(sound, policy)
    factors = expected_factors(sound, include_dividends=convention == CONVENTION_TOTAL_RETURN)
    findings.extend(detect_adjusted_inconsistency(security_id, sound, factors, policy))
    findings.extend(detect_series_anomalies(security_id, unique, policy))
    findings.extend(detect_forward_return_anomalies(security_id, unique, policy))
    return findings, convention
