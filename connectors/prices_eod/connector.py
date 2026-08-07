"""Alpha Vantage EOD prices connector — fetches daily candles for a symbol list."""
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


class PricesEodConnector(BaseConnector):
    source_id = "prices_eod"
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
        outputsize: str = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._symbols = symbols      # None → read from OneLake universe file at fetch time
        self._since_date = since_date  # YYYY-MM-DD override; bypasses watermark when set
        self._to_date = to_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        self._symbol_limit = int(symbol_limit) if symbol_limit is not None else None
        self._outputsize = outputsize or (source_config or {}).get("outputsize") or "compact"
        if self._outputsize not in {"compact", "full"}:
            raise ValueError("outputsize must be 'compact' or 'full'")
        env_rpm = os.environ.get("AV_RPM")
        self._requests_per_minute_value = int(env_rpm) if env_rpm else self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        self._min_interval_s = 60 / self._requests_per_minute_value

    def fetch(self, since: Optional[Watermark]) -> Batch:
        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        to_date = date.fromisoformat(self._to_date) if self._to_date else date.today()
        new_wm = Watermark(source_id=self.source_id)
        if from_date > to_date:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=self._window_id(from_date, to_date, [], 0),
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=False,
            )

        symbols = self._symbols if self._symbols is not None else [
            *self._bw.read_universe("alpha_vantage", "coverage"),
            *self._bw.read_portfolio_universe(),
        ]
        symbols = sorted({str(symbol).upper() for symbol in symbols if symbol})
        total_symbols = len(symbols)
        has_more = False
        if self._symbols is None and (self._symbol_offset or self._symbol_limit is not None):
            end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
            symbols = symbols[self._symbol_offset:end]
            has_more = self._symbol_offset + len(symbols) < total_symbols

        window = self._window_id(from_date, to_date, symbols, total_symbols)

        if not symbols:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=window,
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=has_more,
            )

        records = []
        landed_symbols = set()
        print(
            f"Fetching {len(symbols)} prices_eod symbols "
            f"(offset={self._symbol_offset}, total_universe={total_symbols}, rpm={self._requests_per_minute_value})"
        )
        for symbol in symbols:
            t0 = time.monotonic()
            resp = http_get(
                _AV_URL,
                params={
                    "function": "TIME_SERIES_DAILY",
                    "symbol": symbol,
                    "outputsize": self._outputsize,
                    "apikey": self._api_key,
                },
            )
            data = resp.json()
            series = data.get("Time Series (Daily)", {})
            provider_error = next(
                (
                    str(data.get(field) or "").strip()
                    for field in ("Error Message", "Information", "Note")
                    if str(data.get(field) or "").strip()
                ),
                "",
            )
            if provider_error and not series:
                raise RuntimeError(
                    f"Alpha Vantage price response failed for {symbol}: {provider_error}"
                )
            elapsed = time.monotonic() - t0
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)
            for day_str, ohlcv in series.items():
                day = date.fromisoformat(day_str)
                if day < from_date or day > to_date:
                    continue
                landed_symbols.add(symbol)
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

        if not records:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=window,
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=has_more,
            )
        missing_symbols = sorted(set(symbols) - landed_symbols)
        if missing_symbols:
            raise RuntimeError(
                "Alpha Vantage price landing is incomplete: "
                f"landed={len(landed_symbols)}, expected={len(symbols)}, "
                f"missing={','.join(missing_symbols[:10])}"
            )
        latest_event_date = max(record["date"] for record in records)
        return Batch(
            records=records,
            new_wm=Watermark(
                source_id=self.source_id,
                last_event_ts=latest_event_date,
                last_cursor=latest_event_date,
            ),
            window=window,
            partition_date=to_date.isoformat(),
            watermark_from=from_date.isoformat(),
            has_more=has_more,
        )

    def _window_id(self, from_date: date, to_date: date, symbols: list, total_symbols: int) -> str:
        if not symbols:
            return f"{from_date}-to-{to_date}-symbols-empty-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}"
        digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16]
        return (
            f"{from_date}-to-{to_date}"
            f"-symbols-{len(symbols)}-of-{total_symbols}"
            f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-{digest}"
            f"-outputsize-{self._outputsize}"
        )

    def after_bronze_write(self, batch: Batch) -> None:
        latest_by_symbol: dict[str, dict] = {}
        for record in batch.records:
            symbol = str(record["symbol"]).upper()
            current = latest_by_symbol.get(symbol)
            if current is None or record["date"] > current["date"]:
                latest_by_symbol[symbol] = record
        for symbol, record in latest_by_symbol.items():
            self._cp.upsert_market_data({
                "id": f"quote:{symbol}",
                "ticker": symbol,
                "price": format(record["close"], ".6f"),
                "currency": "USD",
                "as_of": record["date"],
                "source_id": self.source_id,
            })
