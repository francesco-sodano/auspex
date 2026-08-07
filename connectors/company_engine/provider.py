"""Fresh external data packet provider for the company opportunity engine."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import time

import httpx


class FreshCompanyProvider:
    def __init__(
        self,
        *,
        alpha_vantage_api_key: str,
        finnhub_api_key: str,
        http_client=None,
        requests_per_minute: int = 75,
    ) -> None:
        if not alpha_vantage_api_key or not finnhub_api_key:
            raise ValueError("Alpha Vantage and Finnhub API keys are required")
        self.alpha_key = alpha_vantage_api_key
        self.finnhub_key = finnhub_api_key
        self.http = http_client or httpx.Client(timeout=60)
        self.minimum_interval = 60.0 / max(1, requests_per_minute)
        self._last_alpha_request = 0.0

    def fetch_company(self, company: dict, as_of: date) -> dict:
        ticker = str(company["ticker"]).upper()
        errors = []
        prices = self._optional_alpha(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": ticker,
                "outputsize": "compact",
            },
            errors,
            "prices",
        )
        overview = self._optional_alpha(
            {"function": "OVERVIEW", "symbol": ticker},
            errors,
            "overview",
        )
        insider = self._optional_alpha(
            {"function": "INSIDER_TRANSACTIONS", "symbol": ticker},
            errors,
            "insider_transactions",
        )
        from_date = as_of - timedelta(days=59)
        news = self._optional_finnhub(
            "/company-news",
            {
                "symbol": ticker,
                "from": from_date.isoformat(),
                "to": as_of.isoformat(),
                "token": self.finnhub_key,
            },
            errors,
            "news",
        )
        return {
            "company": company,
            "as_of": as_of.isoformat(),
            "prices": _price_rows(prices, as_of),
            "overview": overview if isinstance(overview, dict) else {},
            "insider_transactions": _insider_rows(insider, as_of),
            "news": _news_rows(news, from_date, as_of),
            "errors": errors,
        }

    def fetch_fx(self, from_currency: str, to_currency: str) -> dict | None:
        errors = []
        payload = self._optional_alpha(
            {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_currency,
                "to_currency": to_currency,
            },
            errors,
            f"fx:{from_currency}{to_currency}",
        )
        row = (payload or {}).get("Realtime Currency Exchange Rate") or {}
        rate = row.get("5. Exchange Rate")
        if not rate:
            return None
        return {
            "pair": f"{from_currency.upper()}{to_currency.upper()}",
            "rate": str(rate),
            "as_of": str(row.get("6. Last Refreshed") or date.today().isoformat())[:10],
        }

    def _optional_alpha(self, params, errors, label):
        try:
            return self._alpha(params)
        except Exception as exc:
            errors.append({"source": label, "error": " ".join(str(exc).split())[:500]})
            return {}

    def _alpha(self, params):
        elapsed = time.monotonic() - self._last_alpha_request
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        payload = self._request_json(
            "https://www.alphavantage.co/query",
            {**params, "apikey": self.alpha_key},
        )
        self._last_alpha_request = time.monotonic()
        for field in ("Error Message", "Note", "Information"):
            if payload.get(field):
                raise RuntimeError(f"Alpha Vantage returned {field}: {payload[field]}")
        return payload

    def _optional_finnhub(self, path, params, errors, label):
        try:
            return self._request_json(f"https://finnhub.io/api/v1{path}", params)
        except Exception as exc:
            errors.append({"source": label, "error": " ".join(str(exc).split())[:500]})
            return []

    def _request_json(self, url, params):
        response = None
        for attempt in range(6):
            response = self.http.get(url, params=params)
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                break
            if attempt < 5:
                time.sleep(min(2 ** attempt, 8))
        if response is None:
            raise RuntimeError("provider request returned no response")
        response.raise_for_status()
        return response.json()


def _price_rows(payload: dict, as_of: date) -> list[dict]:
    series = payload.get("Time Series (Daily)") or {}
    rows = []
    for date_text, values in series.items():
        event_date = date.fromisoformat(date_text)
        if event_date > as_of:
            continue
        rows.append({
            "date": date_text,
            "close": _number(values.get("5. adjusted close") or values.get("4. close")),
            "volume": _number(values.get("6. volume") or values.get("5. volume")),
        })
    return sorted(rows, key=lambda row: row["date"], reverse=True)[:30]


def _insider_rows(payload: dict, as_of: date) -> list[dict]:
    values = payload.get("data") or payload.get("transactions") or []
    threshold = as_of - timedelta(days=89)
    rows = []
    for value in values:
        date_text = value.get("transaction_date") or value.get("transactionDate")
        if not date_text:
            continue
        event_date = date.fromisoformat(str(date_text)[:10])
        if event_date < threshold or event_date > as_of:
            continue
        rows.append({
            "date": event_date.isoformat(),
            "acquisition_or_disposal": str(
                value.get("acquisition_or_disposal")
                or value.get("acquisitionOrDisposition")
                or value.get("transaction_type")
                or ""
            ).upper(),
            "shares": _number(value.get("shares")),
            "share_price": _number(value.get("share_price") or value.get("sharePrice")),
        })
    return sorted(rows, key=lambda row: row["date"], reverse=True)


def _news_rows(values, from_date: date, as_of: date) -> list[dict]:
    if not isinstance(values, list):
        return []
    rows = []
    for value in values:
        timestamp = value.get("datetime")
        if timestamp is None:
            continue
        event_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        if event_date < from_date or event_date > as_of:
            continue
        rows.append({
            "id": str(value.get("id") or value.get("url") or timestamp),
            "date": event_date.isoformat(),
            "headline": str(value.get("headline") or "").strip(),
            "summary": str(value.get("summary") or "").strip(),
            "source": str(value.get("source") or "Finnhub"),
            "url": value.get("url"),
        })
    return sorted(rows, key=lambda row: (row["date"], row["id"]), reverse=True)


def _number(value):
    if value in (None, "", "None", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None
