"""Alpha Vantage price/FX provider — default `PriceProvider` + `FxProvider`.

Alpha Vantage is the default implementation. Swapping to Tiingo or another
licensed vendor requires no change outside :mod:`auspex.providers`:
:class:`TiingoPriceProvider` and
:class:`~auspex.providers.fx_provider.ExchangeRateFxProvider` remain
available behind the exact same :class:`PriceProvider`/:class:`FxProvider`
interfaces.

One instance of :class:`AlphaVantageProvider` satisfies both interfaces,
since Alpha Vantage serves both equities and FX from the same API/key.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx

from auspex.currency.money import to_decimal
from auspex.providers.base import FxRateDTO, PriceBarDTO
from auspex.providers.rate_limit import TokenBucket

# Alpha Vantage's free tier is 5 requests/minute; the standard tier is higher.
# 0.08 req/s keeps a free-tier key well under the ceiling without configuration.
DEFAULT_RATE_LIMIT_PER_SECOND = 5 / 60


class AlphaVantageProvider:
    """Implements both `PriceProvider` and `FxProvider` against Alpha Vantage.

    Daily EOD OHLCV comes from ``TIME_SERIES_DAILY_ADJUSTED`` (split/dividend
    adjustment factors exposed, per arc42 §3.1's provider contract); USD/CHF
    comes from ``FX_DAILY``. If the provisioned key's plan does not include
    the adjusted-equities endpoint, ``get_daily_prices`` raises and the
    calling collector marks that security's ingestion degraded for the day
    rather than aborting the run (arc42 §6.1) — the FX side is unaffected,
    since ``FX_DAILY`` is available on every Alpha Vantage plan.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        rate_limit_per_second: float = DEFAULT_RATE_LIMIT_PER_SECOND,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._bucket = TokenBucket(rate_limit_per_second)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, params: dict) -> dict:
        await self._bucket.acquire()
        response = await self._client.get(self._base_url + "/query", params={**params, "apikey": self._api_key})
        response.raise_for_status()
        payload = response.json()
        # Alpha Vantage returns HTTP 200 with an error/notice body on bad
        # requests, throttling, or a plan that lacks the requested function.
        if "Error Message" in payload:
            raise ValueError(f"Alpha Vantage error: {payload['Error Message']}")
        if "Note" in payload:
            raise ValueError(f"Alpha Vantage rate limited: {payload['Note']}")
        if "Information" in payload:
            raise ValueError(f"Alpha Vantage: {payload['Information']}")
        return payload

    async def get_daily_prices(self, ticker: str, since: date) -> list[PriceBarDTO]:
        payload = await self._get(
            {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": ticker, "outputsize": "full"}
        )
        series = payload.get("Time Series (Daily)", {})
        bars = []
        for day_str, row in series.items():
            session_date = datetime.strptime(day_str, "%Y-%m-%d").date()
            if session_date < since:
                continue
            bars.append(self._to_price_dto(ticker, session_date, row))
        return sorted(bars, key=lambda b: b.session_date)

    @staticmethod
    def _to_price_dto(ticker: str, session_date: date, row: dict) -> PriceBarDTO:
        close_raw = to_decimal(row["4. close"])
        adj_close = to_decimal(row.get("5. adjusted close", row["4. close"]))
        adjustment_factor = (adj_close / close_raw) if close_raw != 0 else to_decimal(1)
        return PriceBarDTO(
            ticker=ticker,
            session_date=session_date,
            open_raw=to_decimal(row["1. open"]),
            high_raw=to_decimal(row["2. high"]),
            low_raw=to_decimal(row["3. low"]),
            close_raw=close_raw,
            volume=int(float(row.get("6. volume", 0))),
            close_adjusted=adj_close,
            adjustment_factor=adjustment_factor,
            split_factor=to_decimal(row.get("8. split coefficient", 1)),
            dividend_amount=to_decimal(row.get("7. dividend amount", 0)),
        )

    async def get_usd_chf(self, since: date) -> list[FxRateDTO]:
        payload = await self._get(
            {"function": "FX_DAILY", "from_symbol": "USD", "to_symbol": "CHF", "outputsize": "full"}
        )
        series = payload.get("Time Series FX (Daily)", {})
        rates = []
        for day_str, row in series.items():
            session_date = datetime.strptime(day_str, "%Y-%m-%d").date()
            if session_date < since:
                continue
            rates.append(FxRateDTO(pair="USDCHF", session_date=session_date, close_rate=to_decimal(row["4. close"])))
        return sorted(rates, key=lambda r: r.session_date)
