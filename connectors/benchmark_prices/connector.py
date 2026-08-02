"""Alpha Vantage adjusted daily benchmark prices connector."""
import hashlib
import os
import time
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_REQUESTS_PER_MINUTE = 5
_FUNCTION_NAME = "TIME_SERIES_DAILY_ADJUSTED"
_SERIES_KEY = "Time Series (Daily)"


class BenchmarkPricesConnector(BaseConnector):
    source_id = "benchmark_prices"
    schema_version = 1

    def __init__(
        self,
        cp,
        bw,
        symbols: list = None,
        etf_symbols: list = None,
        since_date: str = None,
        to_date: str = None,
        symbol_offset: int = 0,
        symbol_limit: int = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._symbols = symbols
        self._etf_symbols = etf_symbols if etf_symbols is not None else (source_config or {}).get("etf_symbols") or []
        self._since_date = since_date
        self._to_date = to_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        self._symbol_limit = int(symbol_limit) if symbol_limit is not None else None
        env_rpm = os.environ.get("AV_RPM")
        self._requests_per_minute_value = int(env_rpm) if env_rpm else self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        self._min_interval_s = 60 / self._requests_per_minute_value

    def fetch(self, since: Optional[Watermark]) -> Batch:
        symbol_source = self._symbols if self._symbols is not None else self._etf_symbols
        all_symbols = sorted({str(symbol).upper() for symbol in symbol_source if symbol})
        total_symbols = len(all_symbols)
        end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
        symbols = all_symbols[self._symbol_offset:end]
        has_more = self._symbol_offset + len(symbols) < total_symbols

        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        to_date = date.fromisoformat(self._to_date) if self._to_date else date.today()

        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=to_date.isoformat(),
            last_cursor=to_date.isoformat(),
        )
        window = self._window_id(from_date, to_date, symbols, total_symbols)
        if not symbols or from_date > to_date:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=window,
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=has_more,
            )

        records = []
        for symbol in symbols:
            started_at = time.monotonic()
            payload = http_get(
                _AV_URL,
                params={
                    "function": _FUNCTION_NAME,
                    "symbol": symbol,
                    "outputsize": "full",
                    "apikey": self._api_key,
                },
            ).json()
            elapsed = time.monotonic() - started_at
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)

            self._raise_on_provider_message(payload)
            series = payload.get(_SERIES_KEY)
            if not isinstance(series, dict):
                raise RuntimeError(f"Alpha Vantage {_FUNCTION_NAME} response is missing {_SERIES_KEY}")

            for day_str, observation in sorted(series.items()):
                day = date.fromisoformat(day_str)
                if day < from_date or day > to_date:
                    continue
                records.append({
                    "symbol": symbol,
                    "date": day_str,
                    "open": float(observation["1. open"]),
                    "high": float(observation["2. high"]),
                    "low": float(observation["3. low"]),
                    "close": float(observation["4. close"]),
                    "adjusted_close": float(observation["5. adjusted close"]),
                    "volume": int(observation["6. volume"]),
                    "dividend_amount": float(observation["7. dividend amount"]),
                    "split_coefficient": float(observation["8. split coefficient"]),
                })

        return Batch(
            records=records,
            new_wm=new_wm,
            window=window,
            partition_date=to_date.isoformat(),
            watermark_from=from_date.isoformat(),
            has_more=has_more,
        )

    def _raise_on_provider_message(self, payload: dict) -> None:
        for key in ("Error Message", "Note", "Information"):
            if key in payload:
                raise RuntimeError(f"Alpha Vantage {_FUNCTION_NAME} returned {key}: {payload[key]}")

    def _window_id(self, from_date: date, to_date: date, symbols: list, total_symbols: int) -> str:
        symbol_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16] if symbols else "empty"
        limit = self._symbol_limit if self._symbol_limit is not None else "all"
        return (
            f"{from_date}-to-{to_date}"
            f"-symbols-{len(symbols)}-of-{total_symbols}"
            f"-offset-{self._symbol_offset}-limit-{limit}-{symbol_digest}"
            "-function-TIME_SERIES_DAILY_ADJUSTED-outputsize-full"
        )