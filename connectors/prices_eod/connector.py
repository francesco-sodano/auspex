"""Finnhub EOD prices connector — fetches daily candles for a symbol list."""
import os
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

_FINNHUB_CANDLE_URL = "https://finnhub.io/api/v1/stock/candle"
_DEFAULT_LOOKBACK_DAYS = 7


class PricesEodConnector(BaseConnector):
    source_id = "prices_eod"
    schema_version = 1

    def __init__(self, cp, bw, symbols: list) -> None:
        super().__init__(cp, bw)
        self._api_key = os.environ["FINNHUB_API_KEY"]
        self._symbols = symbols

    def fetch(self, since: Optional[Watermark]) -> Batch:
        if since and since.last_event_ts:
            # fetch from the day after the last known date
            from_date = (
                date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
            )
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

        to_date = date.today()

        if from_date > to_date:
            new_wm = Watermark(source_id=self.source_id, last_event_ts=to_date.isoformat(), last_cursor=to_date.isoformat())
            return Batch(records=[], new_wm=new_wm, window=f"{from_date}-to-{to_date}")

        from_ts = _to_unix(from_date)
        to_ts = _to_unix(to_date)

        records = []
        for symbol in self._symbols:
            resp = http_get(
                _FINNHUB_CANDLE_URL,
                params={
                    "symbol": symbol,
                    "resolution": "D",
                    "from": from_ts,
                    "to": to_ts,
                    "token": self._api_key,
                },
            )
            data = resp.json()
            if data.get("s") != "ok":
                continue
            for i, ts in enumerate(data["t"]):
                records.append({
                    "symbol": symbol,
                    "date": date.fromtimestamp(ts).isoformat(),
                    "open": data["o"][i],
                    "high": data["h"][i],
                    "low": data["l"][i],
                    "close": data["c"][i],
                    "volume": data["v"][i],
                    "adj_close": data["c"][i],  # Finnhub free tier: no adj_close; use close
                })

        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=to_date.isoformat(),
            last_cursor=to_date.isoformat(),
        )
        return Batch(records=records, new_wm=new_wm, window=f"{from_date}-to-{to_date}")


def _to_unix(d: date) -> int:
    from datetime import datetime, timezone
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
