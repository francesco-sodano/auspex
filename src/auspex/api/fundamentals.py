"""One provider-backed overview for Analysis and date-bounded conversation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from auspex.api.schemas import FundamentalMetricOut, FundamentalsContext
from auspex.models.company_overview import CompanyOverviewSnapshot

_METRICS = (
    ("RevenueTTM", "Revenue (TTM)", "money", "Trailing twelve months"),
    (
        "QuarterlyRevenueGrowthYOY",
        "Revenue growth (YoY)",
        "percent",
        "Provider's latest reporting period versus a year earlier",
    ),
    ("GrossProfitTTM", "Gross profit (TTM)", "money", "Trailing twelve months"),
    ("OperatingMarginTTM", "Operating margin (TTM)", "percent", "Trailing twelve months"),
    ("ProfitMargin", "Profit margin (TTM)", "percent", "Trailing twelve months"),
    ("ReturnOnEquityTTM", "Return on equity (TTM)", "percent", "Provider-calculated return on equity, not Auspex ROIC"),
    ("PERatio", "P / E (TTM)", "multiple", "Provider trailing earnings multiple"),
    ("EVToRevenue", "EV / Revenue", "multiple", "Provider enterprise-value / revenue multiple"),
    ("EVToEBITDA", "EV / EBITDA", "multiple", "Provider enterprise-value / EBITDA multiple"),
)


def _display(value: str, kind: str, currency: str | None) -> str:
    amount = Decimal(value)
    if kind == "percent":
        return f"{amount * 100:.1f}%"
    if kind == "multiple":
        return f"{amount:.2f}x"
    if abs(amount) >= Decimal("1000000000"):
        return f"{currency} {amount / Decimal('1000000000'):.2f}B"
    if abs(amount) >= Decimal("1000000"):
        return f"{currency} {amount / Decimal('1000000'):.1f}M"
    return f"{currency} {amount:,.0f}"


def provider_fundamentals(
    snapshot: CompanyOverviewSnapshot | None,
    *,
    now: datetime,
    requested_date: date | None = None,
) -> tuple[list[FundamentalMetricOut], FundamentalsContext]:
    unavailable = "The provider has not supplied a usable current overview for this company."
    if snapshot is not None and requested_date is not None and snapshot.retrieved_at.date() > requested_date:
        snapshot = None
        unavailable = (
            "No provider snapshot was retained on or before the requested date; current values are not backdated."
        )
    if snapshot is not None and snapshot.retrieved_at > now:
        snapshot = None
        unavailable = "The stored provider snapshot has an invalid future retrieval time."
    if snapshot is None:
        context = FundamentalsContext(note=unavailable)
    else:
        age = now - snapshot.retrieved_at
        status = "stale" if age > timedelta(hours=48) else "available"
        note = (
            "Current provider-calculated figures, separate from Auspex score inputs. "
            "TTM means trailing twelve months."
        )
        if status == "stale":
            note = "The provider refresh is overdue; the last successful snapshot is shown. " + note
        context = FundamentalsContext(
            retrieved_at=snapshot.retrieved_at, latest_quarter=snapshot.latest_quarter, status=status, note=note
        )
    metrics = []
    for field, label, kind, basis in _METRICS:
        value = snapshot.metrics.get(field) if snapshot is not None else None
        reason = snapshot.unavailable_fields.get(field) if snapshot is not None else unavailable
        if snapshot is not None and value is not None and kind == "money" and snapshot.financial_currency is None:
            value = None
            reason = reason or "The provider's financial reporting currency could not be verified."
        metrics.append(FundamentalMetricOut(
            label=label,
            value=(
                _display(value, kind, snapshot.financial_currency)
                if snapshot is not None and value is not None else None
            ),
            period_end=snapshot.latest_quarter if snapshot is not None else None,
            detail=basis if value is not None else reason or "Not reported by the provider.",
        ))
    return metrics, context
