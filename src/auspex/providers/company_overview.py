"""Parse a fixed set of current Alpha Vantage OVERVIEW metrics without HTTP.

Currency confirmation never replaces or certifies provider accounting totals.
First, same-issuer statement currency labels from the 366 days ending at
LatestQuarter may confirm a valid quote currency. At least one explicit recent
label is required, and all nonmissing recent quarterly/annual labels must agree.
Malformed recent labels or invalid/future period dates cannot authorize consensus.

Otherwise, strict numerical reconciliation uses RevenueTTM as its primary anchor;
GrossProfitTTM is used only when overview revenue is missing or invalid. The
selected anchor must match within 0.01% of its overview value; zero requires an
exact zero. A valid revenue mismatch never falls back to gross profit. Quarterly
evidence needs four distinct periods ending at LatestQuarter, with 80--100-day
intervals to allow calendar and 52/53-week fiscal quarters. Alternatively a full
annual report must end exactly at LatestQuarter and match the same anchor.

LatestQuarter describes a reporting period, not when a value became known.
These snapshots must not be used to reconstruct historical fundamentals.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import MAX_EMAX, MIN_EMIN, Decimal, DecimalException, InvalidOperation, localcontext

from auspex.models.company_overview import CompanyOverviewSnapshot

logger = logging.getLogger(__name__)

OVERVIEW_METRIC_FIELDS = (
    "RevenueTTM",
    "GrossProfitTTM",
    "QuarterlyRevenueGrowthYOY",
    "QuarterlyEarningsGrowthYOY",
    "ProfitMargin",
    "OperatingMarginTTM",
    "ReturnOnEquityTTM",
    "ReturnOnAssetsTTM",
    "PERatio",
    "TrailingPE",
    "EVToRevenue",
    "EVToEBITDA",
    "MarketCapitalization",
    "EPS",
    "DilutedEPSTTM",
)

_MONEY_FIELDS = ("RevenueTTM", "GrossProfitTTM")
_PE_FIELDS = frozenset(("PERatio", "TrailingPE"))
_ERROR_FIELDS = frozenset(("Error Message", "Error", "error", "Note", "Information"))
_MISSING = object()
_SENTINELS = frozenset(("", "none", "null", "n/a", "na", "#n/a", "-", "--", "not available"))
_DECIMAL_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RELATIVE_TOLERANCE = Decimal("0.0001")
_MAX_COMPARISON_PRECISION = 4096
_CURRENCY_CONSENSUS_DAYS = 366


@dataclass(frozen=True)
class _ParsedNumber:
    value: Decimal | None
    reason: str | None = None

    @property
    def text(self) -> str | None:
        return str(self.value) if self.value is not None else None


@dataclass(frozen=True)
class _DatedReport:
    end: date
    payload: dict


@dataclass(frozen=True)
class _CurrencyProof:
    currency: str | None
    reason: str | None = None


def _missing(value: object) -> bool:
    return value is _MISSING or value is None or isinstance(value, str) and value.strip().lower() in _SENTINELS


def _parse_number(value: object, field: str, *, warn: bool = True) -> _ParsedNumber:
    if _missing(value):
        return _ParsedNumber(None, "not supplied by Alpha Vantage")

    reason = "invalid numeric value"
    if not isinstance(value, bool) and isinstance(value, str | int | float | Decimal):
        text = str(value).strip()
        try:
            number = Decimal(text)
        except InvalidOperation:
            pass
        else:
            if not number.is_finite():
                reason = "non-finite numeric value"
            elif _DECIMAL_PATTERN.fullmatch(text) is not None:
                return _ParsedNumber(number)

    if warn:
        logger.warning("Alpha Vantage field %s is unavailable: %s", field, reason)
    return _ParsedNumber(None, reason)


def _symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Alpha Vantage symbol must be a nonempty string")
    normalized = value.strip().upper()
    if not normalized or any(character.isspace() or ord(character) < 32 for character in normalized):
        raise ValueError("Alpha Vantage symbol must be nonempty and contain no whitespace")
    return normalized


def _period(value: object, field: str) -> date | None:
    if _missing(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == text:
                return parsed
    raise ValueError(f"Alpha Vantage {field} must be an ISO YYYY-MM-DD date")


def _overview_header(payload: dict) -> tuple[str, date | None]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Alpha Vantage OVERVIEW must be a nonempty object")
    if _ERROR_FIELDS.intersection(payload):
        raise ValueError("Alpha Vantage OVERVIEW returned an error or notice")
    return _symbol(payload.get("Symbol")), _period(payload.get("LatestQuarter"), "LatestQuarter")


def _currency(value: object) -> str | None:
    if isinstance(value, str):
        code = value.strip()
        if re.fullmatch(r"[A-Z]{3}", code) is not None and code not in {"XXX", "XTS"}:
            return code
    return None


def _cached_currency(
    symbol: str,
    latest_quarter: date | None,
    amounts: dict[str, _ParsedNumber],
    previous: CompanyOverviewSnapshot | None,
) -> str | None:
    if (
        previous is None
        or previous.financial_currency is None
        or latest_quarter is None
        or previous.latest_quarter != latest_quarter
        or previous.ticker != symbol
        or any(field in previous.unavailable_fields for field in (*_MONEY_FIELDS, "financial_currency"))
    ):
        return None
    for field in _MONEY_FIELDS:
        current = amounts[field].value
        stored = previous.metrics.get(field)
        if current is None or stored is None or current != Decimal(stored):
            return None
    return previous.financial_currency


def needs_income_statement(payload: dict, previous: CompanyOverviewSnapshot | None) -> bool:
    """Skip a statement request only for the same previously verified TTM data.

    Equality is numeric (not a rounding tolerance); symbol and period must
    also match. Invalid overview envelopes raise before a second API call.
    The caller must load ``previous`` from the requested security's partition.
    """

    symbol, latest_quarter = _overview_header(payload)
    amounts = {field: _parse_number(payload.get(field), field, warn=False) for field in _MONEY_FIELDS}
    return _cached_currency(symbol, latest_quarter, amounts, previous) is None


def _report_dates(raw: object, kind: str, retrieved_on: date) -> tuple[list[_DatedReport], str | None]:
    if not isinstance(raw, list) or not raw:
        return [], f"{kind} reports are missing or invalid"
    reports: list[_DatedReport] = []
    seen: set[date] = set()
    for row in raw:
        if not isinstance(row, dict):
            return [], f"{kind} report must be an object"
        try:
            end = _period(row.get("fiscalDateEnding"), "fiscalDateEnding")
        except ValueError:
            return [], f"{kind} report has an invalid fiscalDateEnding"
        if end is None or end > retrieved_on or end in seen:
            return [], f"{kind} reports have missing, future, or duplicate period ends"
        seen.add(end)
        reports.append(_DatedReport(end, row))
    return sorted(reports, key=lambda report: report.end, reverse=True), None


def _currency_consensus(
    statement: dict,
    *,
    quote_currency: str | None,
    latest_quarter: date,
    retrieved_on: date,
) -> _CurrencyProof:
    if quote_currency is None:
        return _CurrencyProof(None, "currency consensus requires a valid quote currency")

    explicit_evidence = False
    for field, kind in (("quarterlyReports", "quarterly"), ("annualReports", "annual")):
        raw = statement.get(field)
        if raw is None or isinstance(raw, list) and not raw:
            continue
        reports, error = _report_dates(raw, kind, retrieved_on)
        if error is not None:
            return _CurrencyProof(None, f"currency consensus rejected: {error}")
        for report in reports:
            if report.end > latest_quarter:
                return _CurrencyProof(None, "currency consensus has a future period after LatestQuarter")
            if (latest_quarter - report.end).days > _CURRENCY_CONSENSUS_DAYS:
                continue
            label = report.payload.get("reportedCurrency")
            if _missing(label):
                continue
            currency = _currency(label)
            if currency is None:
                return _CurrencyProof(None, "currency consensus has a malformed recent reportedCurrency")
            if currency != quote_currency:
                return _CurrencyProof(None, "recent reportedCurrency does not agree with quote currency")
            explicit_evidence = True

    if not explicit_evidence:
        return _CurrencyProof(None, "no explicit reporting currency within 366 days ending at LatestQuarter")
    return _CurrencyProof(quote_currency)


def _prove_totals(reports: list[_DatedReport], amounts: dict[str, _ParsedNumber]) -> _CurrencyProof:
    currencies = {_currency(report.payload.get("reportedCurrency")) for report in reports}
    if None in currencies or len(currencies) != 1:
        return _CurrencyProof(None, "selected statements lack one consistent reportedCurrency")

    field, statement_field = (
        ("RevenueTTM", "totalRevenue") if amounts["RevenueTTM"].value is not None else ("GrossProfitTTM", "grossProfit")
    )
    expected = amounts[field].value
    reported = [
        _parse_number(report.payload.get(statement_field), f"INCOME_STATEMENT.{statement_field}") for report in reports
    ]
    if expected is None or any(number.value is None for number in reported):
        return _CurrencyProof(None, f"{field} and corresponding statement values must be finite and available")

    finite = [expected, *(number.value for number in reported if number.value is not None)]
    # Exact addition/subtraction needs the complete exponent span, including
    # cancellation. Bound that work instead of rounding away a zero mismatch.
    precision = max(
        28, max(number.adjusted() for number in finite) - min(int(number.as_tuple().exponent) for number in finite) + 8
    )
    if precision > _MAX_COMPARISON_PRECISION:
        return _CurrencyProof(None, "statement comparison exceeds supported decimal precision")
    try:
        with localcontext() as context:
            context.prec = precision
            context.Emax = MAX_EMAX
            context.Emin = MIN_EMIN
            actual = sum(finite[1:], Decimal(0))
            if abs(actual - expected) > abs(expected) * _RELATIVE_TOLERANCE:
                return _CurrencyProof(None, f"statement {field} totals do not match overview within 0.01%")
    except DecimalException:
        return _CurrencyProof(None, "statement comparison exceeds supported decimal range")
    return _CurrencyProof(next(iter(currencies)))


def _verify_currency(
    statement: dict,
    *,
    symbol: str,
    latest_quarter: date | None,
    quote_currency: str | None,
    amounts: dict[str, _ParsedNumber],
    retrieved_on: date,
) -> _CurrencyProof:
    if not isinstance(statement, dict) or not statement or _ERROR_FIELDS.intersection(statement):
        return _CurrencyProof(None, "income statement is missing, invalid, or an error/notice response")
    try:
        statement_symbol = _symbol(statement.get("symbol"))
    except ValueError:
        return _CurrencyProof(None, "income statement symbol is missing or invalid")
    if statement_symbol != symbol:
        return _CurrencyProof(None, "income statement symbol does not match the overview")
    if latest_quarter is None:
        return _CurrencyProof(None, "LatestQuarter is unavailable")

    consensus = _currency_consensus(
        statement,
        quote_currency=quote_currency,
        latest_quarter=latest_quarter,
        retrieved_on=retrieved_on,
    )
    if consensus.currency is not None:
        return consensus
    if all(amounts[field].value is None for field in _MONEY_FIELDS):
        return _CurrencyProof(
            None,
            f"{consensus.reason}; RevenueTTM or GrossProfitTTM is required for currency reconciliation",
        )

    quarterly, quarter_error = _report_dates(statement.get("quarterlyReports"), "quarterly", retrieved_on)
    selected = [report for report in quarterly if report.end <= latest_quarter][:4]
    if quarter_error is not None:
        quarterly_proof = _CurrencyProof(None, quarter_error)
    elif len(selected) != 4 or selected[0].end != latest_quarter:
        quarterly_proof = _CurrencyProof(None, "four quarterly reports ending at LatestQuarter are required")
    elif any(
        not 80 <= (newer.end - older.end).days <= 100 for newer, older in zip(selected, selected[1:], strict=False)
    ):
        quarterly_proof = _CurrencyProof(None, "quarterly periods are not four successive fiscal quarters")
    else:
        quarterly_proof = _prove_totals(selected, amounts)

    annual, annual_error = _report_dates(statement.get("annualReports"), "annual", retrieved_on)
    matching_annual = [report for report in annual if report.end == latest_quarter]
    if annual_error is not None:
        annual_proof = _CurrencyProof(None, annual_error)
    elif not matching_annual:
        annual_proof = _CurrencyProof(None, "no full annual report ends at LatestQuarter")
    else:
        annual_proof = _prove_totals(matching_annual, amounts)

    if quarterly_proof.currency is not None and annual_proof.currency is not None:
        if quarterly_proof.currency != annual_proof.currency:
            return _CurrencyProof(None, "quarterly and annual currency proofs disagree")
    if quarterly_proof.currency is not None:
        return quarterly_proof
    if annual_proof.currency is not None:
        return annual_proof
    return _CurrencyProof(None, f"{consensus.reason}; {quarterly_proof.reason}; {annual_proof.reason}")


def build_company_overview(
    payload: dict,
    *,
    security_id: str,
    ticker: str,
    retrieved_at: datetime,
    income_statement: dict | None = None,
    previous: CompanyOverviewSnapshot | None = None,
) -> CompanyOverviewSnapshot:
    """Build a current snapshot; malformed overview envelopes raise ValueError.

    Missing/invalid metrics remain null with reasons. Failed statement proof
    leaves financial_currency null and explains the unknown unit on both TTM
    monetary fields; valid numeric provider values and ratios are retained.
    An explicitly supplied statement is checked even if a cache is reusable.
    No raw response values are included in warnings or error messages.
    """

    symbol, latest_quarter = _overview_header(payload)
    if symbol != _symbol(ticker):
        raise ValueError("Alpha Vantage OVERVIEW symbol does not match the requested ticker")
    if not isinstance(retrieved_at, datetime):
        raise ValueError("retrieved_at must be a datetime")
    if latest_quarter is not None and latest_quarter > retrieved_at.date():
        raise ValueError("LatestQuarter cannot be after the retrieval date")

    parsed = {field: _parse_number(payload.get(field, _MISSING), field) for field in OVERVIEW_METRIC_FIELDS}
    for field in _PE_FIELDS:
        value = parsed[field].value
        if value is not None and value <= 0:
            reason = "non-positive P/E is not meaningful"
            parsed[field] = _ParsedNumber(None, reason)
            logger.warning("Alpha Vantage field %s is unavailable: %s", field, reason)
    unavailable = {field: value.reason for field, value in parsed.items() if value.reason is not None}
    quote_currency = _currency(payload.get("Currency"))
    if quote_currency is None:
        unavailable["Currency"] = "quote currency is missing or invalid"
    if latest_quarter is None:
        unavailable["LatestQuarter"] = "not supplied by Alpha Vantage"

    if income_statement is not None:
        proof = _verify_currency(
            income_statement,
            symbol=symbol,
            latest_quarter=latest_quarter,
            quote_currency=quote_currency,
            amounts=parsed,
            retrieved_on=retrieved_at.date(),
        )
    else:
        cached = previous if previous is not None and previous.security_id == security_id else None
        currency = _cached_currency(symbol, latest_quarter, parsed, cached)
        proof = _CurrencyProof(
            currency, None if currency is not None else "income statement verification is unavailable"
        )

    if proof.currency is None:
        reason = f"financial currency unverified: {proof.reason}"
        unavailable["financial_currency"] = reason
        for field in _MONEY_FIELDS:
            unavailable[field] = f"{unavailable[field]}; {reason}" if field in unavailable else reason

    return CompanyOverviewSnapshot(
        id=security_id,
        security_id=security_id,
        ticker=symbol,
        retrieved_at=retrieved_at,
        quote_currency=quote_currency,
        financial_currency=proof.currency,
        latest_quarter=latest_quarter,
        metrics={field: value.text for field, value in parsed.items()},
        unavailable_fields=unavailable,
    )
