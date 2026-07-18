"""Finnhub company-news connector for raw article feed coverage."""
import os
import time
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

_FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_MAX_LOOKBACK_DAYS = 365
_DEFAULT_REQUESTS_PER_MINUTE = 60


class NewsConnector(BaseConnector):
    source_id = "news"
    schema_version = 1

    def __init__(
        self,
        cp,
        bw,
        symbols: list = None,
        since_date: str = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["FINNHUB_API_KEY"]
        self._symbols = symbols
        self._since_date = since_date
        self._max_lookback_days = int(os.environ.get("FINNHUB_MAX_LOOKBACK_DAYS") or (source_config or {}).get("max_lookback_days") or _DEFAULT_MAX_LOOKBACK_DAYS)
        self._min_interval_s = 60 / self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)

    def fetch(self, since: Optional[Watermark]) -> Batch:
        to_date = date.today()
        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = to_date - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        from_date = self._bounded_from_date(from_date, to_date)
        symbols = self._symbols if self._symbols is not None else self._bw.read_universe()
        symbols = sorted({str(symbol).upper() for symbol in symbols if symbol})
        records = []

        for symbol in symbols:
            started_at = time.monotonic()
            data = http_get(
                _FINNHUB_URL,
                params={"symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat(), "token": self._api_key},
            ).json()
            elapsed = time.monotonic() - started_at
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)
            for article in data or []:
                records.append({"symbol": symbol, "article": article})

        new_wm = Watermark(source_id=self.source_id, last_event_ts=to_date.isoformat(), last_cursor=to_date.isoformat())
        return Batch(records=records, new_wm=new_wm, window=f"{from_date}-to-{to_date}-symbols-{len(symbols)}", partition_date=to_date.isoformat())

    def _bounded_from_date(self, from_date: date, to_date: date) -> date:
        earliest_allowed = to_date - timedelta(days=self._max_lookback_days)
        return max(from_date, earliest_allowed)
