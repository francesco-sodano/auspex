"""Detector-level tests for :mod:`auspex.marketdata.detect` (arc42 §5.3)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from auspex.marketdata.detect import (
    CONVENTION_SPLIT_ONLY,
    CONVENTION_TOTAL_RETURN,
    dedupe_bars,
    detect_convention,
    detect_forward_return_anomalies,
    detect_series_anomalies,
    diagnose_security,
    evaluate_bar,
    expected_factors,
    looks_like_round_split,
)
from auspex.marketdata.policy import DEFAULT_POLICY
from auspex.models.market import PriceBar
from auspex.models.market_integrity import IntegrityCode, IntegritySeverity

SECURITY = "sec-1"
START = date(2024, 1, 1)


def make_bar(
    offset: int,
    close: str,
    *,
    security_id: str = SECURITY,
    adjusted: str | None = None,
    factor: str | None = None,
    split: str = "1",
    dividend: str = "0",
    volume: int = 1_000,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    bar_id: str | None = None,
) -> PriceBar:
    session = START + timedelta(days=offset)
    close_dec = Decimal(close)
    return PriceBar(
        id=bar_id or f"{security_id}:{session.isoformat()}",
        security_id=security_id,
        session_date=session,
        open_raw=open_ or close,
        high_raw=high or str(close_dec),
        low_raw=low or str(close_dec),
        close_raw=close,
        volume=volume,
        close_adjusted=adjusted if adjusted is not None else close,
        adjustment_factor=factor if factor is not None else "1",
        split_factor=split,
        dividend_amount=dividend,
    )


def codes(findings) -> set[str]:
    return {finding.code.value for finding in findings}


def test_evaluate_bar_accepts_a_sound_bar() -> None:
    assert evaluate_bar(make_bar(0, "10", high="11", low="9", open_="9.5")) == []


def test_evaluate_bar_flags_non_positive_price() -> None:
    findings = evaluate_bar(make_bar(0, "0"))
    assert IntegrityCode.NON_POSITIVE_PRICE.value in codes(findings)
    assert all(f.severity is IntegritySeverity.ERROR for f in findings)


def test_evaluate_bar_flags_inverted_high_low() -> None:
    bar = make_bar(0, "10", high="9", low="11")
    assert IntegrityCode.IMPOSSIBLE_OHLC.value in codes(evaluate_bar(bar))


def test_evaluate_bar_flags_high_below_close() -> None:
    bar = make_bar(0, "10", high="9.5", low="9")
    assert IntegrityCode.IMPOSSIBLE_OHLC.value in codes(evaluate_bar(bar))


def test_evaluate_bar_flags_negative_volume_and_bad_derived_fields() -> None:
    bar = make_bar(0, "10", volume=-1, adjusted="0", factor="-1", split="0", dividend="-1")
    found = codes(evaluate_bar(bar))
    assert IntegrityCode.IMPOSSIBLE_VOLUME.value in found
    assert IntegrityCode.IMPOSSIBLE_ADJUSTED.value in found
    assert IntegrityCode.IMPOSSIBLE_SPLIT_FACTOR.value in found
    assert IntegrityCode.IMPOSSIBLE_DIVIDEND.value in found


def test_dedupe_bars_reports_duplicate_sessions() -> None:
    bars = [
        make_bar(0, "10", bar_id="sec-1:2024-01-01"),
        make_bar(0, "11", bar_id="sec-1:2024-01-01#dup"),
    ]
    unique, findings = dedupe_bars(bars)
    assert len(unique) == 1
    assert codes(findings) == {IntegrityCode.DUPLICATE_BAR.value}


def test_expected_factors_anchor_latest_at_one_and_back_adjust_a_split() -> None:
    bars = [
        make_bar(0, "100"),
        make_bar(1, "50", split="2"),
        make_bar(2, "51"),
    ]
    factors = expected_factors(bars, include_dividends=False)
    assert factors[bars[2].session_date] == Decimal("1")
    assert factors[bars[1].session_date] == Decimal("1")
    assert factors[bars[0].session_date] == Decimal("0.5")


def test_expected_factors_include_dividends_when_requested() -> None:
    bars = [make_bar(0, "100"), make_bar(1, "101", dividend="1")]
    with_div = expected_factors(bars, include_dividends=True)
    without = expected_factors(bars, include_dividends=False)
    assert with_div[bars[0].session_date] == Decimal("0.99")
    assert without[bars[0].session_date] == Decimal("1")


def test_detect_convention_prefers_split_only_when_dividends_are_not_applied() -> None:
    bars = [
        make_bar(0, "100", adjusted="100"),
        make_bar(1, "101", adjusted="101", dividend="1"),
    ]
    convention, score = detect_convention(bars)
    assert convention == CONVENTION_SPLIT_ONLY
    assert score is not None


def test_detect_convention_prefers_total_return_for_dividend_adjusted_series() -> None:
    bars = [
        make_bar(0, "100", adjusted="99"),
        make_bar(1, "101", adjusted="101", dividend="1"),
    ]
    convention, _ = detect_convention(bars)
    assert convention == CONVENTION_TOTAL_RETURN


def test_series_anomaly_reports_split_factor_discontinuity() -> None:
    # split_factor says 2:1 but the raw close did not halve.
    bars = [make_bar(0, "100"), make_bar(1, "100", split="2")]
    findings = detect_series_anomalies(SECURITY, bars)
    assert codes(findings) == {IntegrityCode.SPLIT_FACTOR_DISCONTINUITY.value}
    assert findings[0].severity is IntegritySeverity.ERROR


def test_series_anomaly_reports_unexplained_scale_break_without_inventing_a_split() -> None:
    bars = [make_bar(0, "100"), make_bar(1, "50")]
    findings = detect_series_anomalies(SECURITY, bars)
    assert codes(findings) == {IntegrityCode.UNEXPLAINED_SCALE_BREAK.value}
    assert findings[0].severity is IntegritySeverity.WARNING
    assert "no split is inferred" in findings[0].detail
    assert "no history is quarantined" in findings[0].detail


def test_series_anomaly_accepts_a_correctly_recorded_split() -> None:
    bars = [make_bar(0, "100"), make_bar(1, "50", split="2")]
    assert detect_series_anomalies(SECURITY, bars) == []


def test_series_anomaly_accepts_a_correctly_recorded_reverse_split() -> None:
    bars = [make_bar(0, "10"), make_bar(1, "100", split="0.1")]
    assert detect_series_anomalies(SECURITY, bars) == []


def test_series_anomaly_flags_extreme_jump_as_error() -> None:
    bars = [make_bar(0, "10"), make_bar(1, "1000")]
    findings = detect_series_anomalies(SECURITY, bars)
    assert codes(findings) == {IntegrityCode.IMPLAUSIBLE_JUMP.value}
    assert findings[0].severity is IntegritySeverity.ERROR


def test_series_anomaly_flags_non_round_move_as_warning() -> None:
    bars = [make_bar(0, "100"), make_bar(1, "170")]
    findings = detect_series_anomalies(SECURITY, bars)
    assert codes(findings) == {IntegrityCode.IMPLAUSIBLE_JUMP.value}
    assert findings[0].severity is IntegritySeverity.WARNING


def test_looks_like_round_split_matches_both_directions() -> None:
    assert looks_like_round_split(Decimal("100"), Decimal("50"), DEFAULT_POLICY) == Decimal("2")
    assert looks_like_round_split(Decimal("50"), Decimal("100"), DEFAULT_POLICY) == Decimal("2")
    assert looks_like_round_split(Decimal("100"), Decimal("99"), DEFAULT_POLICY) is None
    # A 1.5x repricing is below min_split_ratio: never treated as a split.
    assert looks_like_round_split(Decimal("150"), Decimal("100"), DEFAULT_POLICY) is None


def test_forward_return_anomaly_is_attributed_to_the_worst_session() -> None:
    # A level shift at index 10 makes every window spanning it implausible.
    bars = [
        make_bar(index, "100", adjusted="100" if index < 10 else "100000") for index in range(30)
    ]
    findings = detect_forward_return_anomalies(SECURITY, bars)
    assert codes(findings) == {IntegrityCode.FORWARD_RETURN_ANOMALY.value}
    assert findings[0].session_date == bars[10].session_date
    assert findings[0].severity is IntegritySeverity.ERROR


def test_forward_return_anomaly_is_silent_on_a_flat_series() -> None:
    bars = [make_bar(index, "100", adjusted="100") for index in range(40)]
    assert detect_forward_return_anomalies(SECURITY, bars) == []


def test_diagnose_security_returns_findings_and_convention() -> None:
    bars = [
        make_bar(0, "100", adjusted="50", factor="0.5"),
        make_bar(1, "50", adjusted="50", factor="1", split="2"),
    ]
    findings, convention = diagnose_security(SECURITY, bars)
    assert convention in {CONVENTION_SPLIT_ONLY, CONVENTION_TOTAL_RETURN}
    assert findings == []


def test_diagnose_security_flags_a_broken_adjusted_series() -> None:
    bars = [
        make_bar(0, "100", adjusted="100", factor="1"),
        make_bar(1, "50", adjusted="50", factor="1", split="2"),
    ]
    findings, _ = diagnose_security(SECURITY, bars)
    assert IntegrityCode.ADJUSTED_SERIES_INCONSISTENT.value in codes(findings)
