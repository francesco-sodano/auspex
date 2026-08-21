"""USD/CHF FX rate provider via exchangerate.host (default `FxProvider`)."""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from auspex.currency.money import to_decimal
from auspex.providers.base import FxRateDTO


class ExchangeRateFxProvider:
    def __init__(self, *, base_url: str, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_usd_chf(self, since: date) -> list[FxRateDTO]:
        return await self.get_daily_fx("USDCHF", since)

    async def get_daily_fx(
        self,
        pair: str,
        since: date,
    ) -> list[FxRateDTO]:
        normalized = pair.strip().upper()
        if len(normalized) != 6 or not normalized.isalpha():
            raise ValueError("FX pair must contain two three-letter currencies")
        base, quote_currency = normalized[:3], normalized[3:]
        url = f"{self._base_url}/timeframe"
        params = {
            "access_key": self._api_key,
            "start_date": since.isoformat(),
            "end_date": date.today().isoformat(),
            "source": base,
            "currencies": quote_currency,
        }
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        rates = payload.get("quotes", {})
        results: list[FxRateDTO] = []
        for day_key, quote_values in rates.items():
            session_date = date.fromisoformat(day_key)
            close_rate = to_decimal(quote_values[normalized])
            results.append(
                FxRateDTO(
                    pair=normalized,
                    session_date=session_date,
                    close_rate=close_rate,
                )
            )
        return sorted(results, key=lambda r: r.session_date)


def business_days_between(start: date, end: date) -> int:
    """Approximate trading-session count, used by watermark logic."""

    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days
