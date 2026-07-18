"""Pure Alpha Vantage response mappers for E8 gold loaders and tests."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

_NULL_STRINGS = {"", "none", "null", "n/a", "na", "-", "--"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_STRINGS:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def to_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_STRINGS:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def av_timestamp_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_STRINGS:
        return None
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return None


def map_overview(symbol: str, payload: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "as_of_date": fetched_at[:10],
        "knowledge_date": fetched_at[:10],
        "currency": payload.get("Currency") or "USD",
        "sector": payload.get("Sector"),
        "industry": payload.get("Industry"),
        "market_cap": to_decimal(payload.get("MarketCapitalization")),
        "ebitda": to_decimal(payload.get("EBITDA")),
        "pe_ratio": to_decimal(payload.get("PERatio")),
        "peg_ratio": to_decimal(payload.get("PEGRatio")),
        "ps_ratio": to_decimal(payload.get("PriceToSalesRatioTTM")),
        "ev_ebitda": to_decimal(payload.get("EVToEBITDA")),
        "gross_margin": to_decimal(payload.get("GrossProfitTTM")),
        "profit_margin": to_decimal(payload.get("ProfitMargin")),
        "rev_growth_yoy": to_decimal(payload.get("QuarterlyRevenueGrowthYOY")),
    }


def latest_quarterly_report(payload: dict[str, Any]) -> dict[str, Any]:
    reports = payload.get("quarterlyReports") or []
    return reports[0] if reports else {}


def map_balance_sheet(symbol: str, payload: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    report = latest_quarterly_report(payload)
    cash = to_decimal(report.get("cashAndCashEquivalentsAtCarryingValue"))
    short_debt = to_decimal(report.get("shortTermDebt")) or Decimal("0")
    long_debt = to_decimal(report.get("longTermDebt")) or Decimal("0")
    total_debt = to_decimal(report.get("shortLongTermDebtTotal"))
    if total_debt is None:
        total_debt = short_debt + long_debt
    return {
        "symbol": symbol.upper(),
        "fiscal_date_ending": str(report.get("fiscalDateEnding") or fetched_at[:10]),
        "knowledge_date": fetched_at[:10],
        "cash_and_equivalents": cash,
        "total_debt": total_debt,
    }


def map_cash_flow(symbol: str, payload: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    report = latest_quarterly_report(payload)
    operating_cashflow = to_decimal(report.get("operatingCashflow"))
    capex = to_decimal(report.get("capitalExpenditures"))
    return {
        "symbol": symbol.upper(),
        "fiscal_date_ending": str(report.get("fiscalDateEnding") or fetched_at[:10]),
        "knowledge_date": fetched_at[:10],
        "operating_cashflow": operating_cashflow,
        "capital_expenditures": capex,
    }


def map_news_sentiment(symbol: str, payload: dict[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    rows = []
    for article in payload.get("feed") or []:
        published_at = av_timestamp_to_iso(article.get("time_published"))
        ticker_sentiment = article.get("ticker_sentiment") or []
        matching = [item for item in ticker_sentiment if str(item.get("ticker", "")).upper() == symbol.upper()]
        for item in matching or [{}]:
            rows.append({
                "symbol": symbol.upper(),
                "news_sk": int(stable_hash(symbol.upper(), article.get("url"), article.get("time_published"))[:15], 16),
                "title_hash": stable_hash(article.get("title"), article.get("url")),
                "title": article.get("title"),
                "url": article.get("url"),
                "source": article.get("source"),
                "summary": article.get("summary"),
                "event_date": (published_at or fetched_at)[:10],
                "knowledge_date": fetched_at[:10],
                "sentiment": to_decimal(item.get("ticker_sentiment_score") or article.get("overall_sentiment_score")),
                "relevance": to_decimal(item.get("relevance_score")),
            })
    return rows


def map_treasury_yield(payload: dict[str, Any], fetched_at: str, maturity: str = "3month") -> list[dict[str, Any]]:
    rows = []
    for point in payload.get("data") or []:
        event_date = str(point.get("date") or "")[:10]
        value = to_decimal(point.get("value"))
        if not event_date or value is None:
            continue
        rows.append({
            "indicator_code": f"US_TREASURY_{maturity.upper()}",
            "event_date": event_date,
            "knowledge_date": fetched_at[:10],
            "value": value,
        })
    return rows


def map_currency_exchange_rate(payload: dict[str, Any], fetched_at: str) -> dict[str, Any] | None:
    rate = payload.get("Realtime Currency Exchange Rate") or {}
    from_currency = rate.get("1. From_Currency Code")
    to_currency = rate.get("3. To_Currency Code")
    value = to_decimal(rate.get("5. Exchange Rate"))
    if not from_currency or not to_currency or value is None:
        return None
    refreshed = rate.get("6. Last Refreshed") or fetched_at[:10]
    return {
        "ccy_pair": f"{from_currency.upper()}{to_currency.upper()}",
        "event_date": str(refreshed)[:10],
        "knowledge_date": fetched_at[:10],
        "rate": value,
    }


def map_etf_profile(symbol: str, payload: dict[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    rows = []
    for holding in payload.get("holdings") or []:
        holding_symbol = str(holding.get("symbol") or "").upper()
        if not holding_symbol:
            continue
        rows.append({
            "theme_id": f"etf:{symbol.upper()}",
            "etf_symbol": symbol.upper(),
            "holding_symbol": holding_symbol,
            "weight": to_decimal(holding.get("weight")),
            "event_date": fetched_at[:10],
            "knowledge_date": fetched_at[:10],
            "is_ground_truth": True,
        })
    return rows
