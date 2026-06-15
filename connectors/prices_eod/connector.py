"""Alpha Vantage EOD prices connector — fetches daily candles for a symbol list."""
import os
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_LOOKBACK_DAYS = 7


class PricesEodConnector(BaseConnector):
    source_id = "prices_eod"
    schema_version = 1

    def __init__(self, cp, bw, symbols: list) -> None:
        super().__init__(cp, bw)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._symbols = symbols

    def fetch(self, since: Optional[Watermark]) -> Batch:
        if since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

        to_date = date.today()

        if from_date > to_date:
            new_wm = Watermark(source_id=self.source_id, last_event_ts=to_date.isoformat(), last_cursor=to_date.isoformat())
            return Batch(records=[], new_wm=new_wm, window=f"{from_date}-to-{to_date}")

        records = []
        for symbol in self._symbols:
            resp = http_get(
                _AV_URL,
                params={
                    "function": "TIME_SERIES_DAILY",
                    "symbol": symbol,
                    "outputsize": "compact",  # last 100 trading days — sufficient for daily runs
                    "apikey": self._api_key,
                },
            )
            data = resp.json()
            series = data.get("Time Series (Daily)", {})
            for day_str, ohlcv in series.items():
                day = date.fromisoformat(day_str)
                if day < from_date or day > to_date:
                    continue
                records.append({
                    "symbol": symbol,
                    "date": day_str,
                    "open": float(ohlcv["1. open"]),
                    "high": float(ohlcv["2. high"]),
                    "low": float(ohlcv["3. low"]),
                    "close": float(ohlcv["4. close"]),
                    "volume": int(ohlcv["5. volume"]),
                    "adj_close": float(ohlcv["4. close"]),  # AV free tier: no adj_close; use close
                })

        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=to_date.isoformat(),
            last_cursor=to_date.isoformat(),
        )
        return Batch(records=records, new_wm=new_wm, window=f"{from_date}-to-{to_date}")
