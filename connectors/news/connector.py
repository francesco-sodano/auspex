"""Finnhub company-news connector for raw article feed coverage."""
import hashlib
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
        to_date: str = None,
        symbol_offset: int = 0,
        symbol_limit: int = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["FINNHUB_API_KEY"]
        self._symbols = symbols
        self._since_date = since_date
        self._to_date = to_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        self._symbol_limit = int(symbol_limit) if symbol_limit is not None else None
        self._max_lookback_days = int(os.environ.get("FINNHUB_MAX_LOOKBACK_DAYS") or (source_config or {}).get("max_lookback_days") or _DEFAULT_MAX_LOOKBACK_DAYS)
        self._min_interval_s = 60 / self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)

    def fetch(self, since: Optional[Watermark]) -> Batch:
        to_date = date.fromisoformat(self._to_date) if self._to_date else date.today()
        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = to_date - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        from_date = self._bounded_from_date(from_date, to_date)
        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=to_date.isoformat(),
            last_cursor=to_date.isoformat(),
        )
        if from_date > to_date:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=(
                    f"{from_date}-to-{to_date}-symbols-0-of-0"
                    f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-empty"
                ),
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=False,
            )

        symbols = self._symbols if self._symbols is not None else [
            *self._bw.read_universe("alpha_vantage", "active"),
            *self._bw.read_portfolio_universe(),
        ]
        symbols = sorted({str(symbol).upper() for symbol in symbols if symbol})
        total_symbols = len(symbols)
        has_more = False
        if self._symbols is None and (self._symbol_offset or self._symbol_limit is not None):
            end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
            symbols = symbols[self._symbol_offset:end]
            has_more = self._symbol_offset + len(symbols) < total_symbols
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

        symbol_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16] if symbols else "empty"
        return Batch(
            records=records,
            new_wm=new_wm,
            window=(
                f"{from_date}-to-{to_date}-symbols-{len(symbols)}-of-{total_symbols}"
                f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-{symbol_digest}"
            ),
            partition_date=to_date.isoformat(),
            watermark_from=from_date.isoformat(),
            has_more=has_more,
        )

    def _bounded_from_date(self, from_date: date, to_date: date) -> date:
        earliest_allowed = to_date - timedelta(days=self._max_lookback_days)
        return max(from_date, earliest_allowed)
