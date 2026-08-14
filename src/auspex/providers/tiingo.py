"""Tiingo daily EOD price provider (default `PriceProvider` implementation).

arc42 §3.1: API key in Key Vault, never a connection string / hard-coded key.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import httpx

from auspex.currency.money import to_decimal
from auspex.providers.base import PriceBarDTO


class TiingoPriceProvider:
    def __init__(self, *, base_url: str, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_daily_prices(self, ticker: str, since: date) -> list[PriceBarDTO]:
        url = f"{self._base_url}/tiingo/daily/{ticker}/prices"
        params = {"startDate": since.isoformat(), "token": self._api_key, "format": "json"}
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        rows = response.json()
        return [self._to_dto(ticker, row) for row in rows]

    @staticmethod
    def _to_dto(ticker: str, row: dict) -> PriceBarDTO:
        session_date = datetime.fromisoformat(row["date"].replace("Z", "+00:00")).date()
        close_raw = to_decimal(row["close"])
        adj_close = to_decimal(row.get("adjClose", row["close"]))
        adjustment_factor = (adj_close / close_raw) if close_raw != 0 else Decimal(1)
        return PriceBarDTO(
            ticker=ticker,
            session_date=session_date,
            open_raw=to_decimal(row["open"]),
            high_raw=to_decimal(row["high"]),
            low_raw=to_decimal(row["low"]),
            close_raw=close_raw,
            volume=int(row.get("volume", 0)),
            close_adjusted=adj_close,
            adjustment_factor=adjustment_factor,
            split_factor=to_decimal(row.get("splitFactor", 1)),
            dividend_amount=to_decimal(row.get("divCash", 0)),
        )
