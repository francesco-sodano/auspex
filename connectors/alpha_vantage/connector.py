"""Alpha Vantage E8 connector for fundamentals, news sentiment, FX, macro, and holdings."""
import hashlib
import os
import time
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

from .mapping import utc_now_iso

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_REQUESTS_PER_MINUTE = 5
_SYMBOL_FUNCTIONS = ["OVERVIEW", "BALANCE_SHEET", "CASH_FLOW", "NEWS_SENTIMENT", "INSTITUTIONAL_HOLDINGS"]


class AlphaVantageConnector(BaseConnector):
    source_id = "alpha_vantage"
    schema_version = 1

    def __init__(
        self,
        cp,
        bw,
        symbols: list = None,
        etf_symbols: list = None,
        since_date: str = None,
        symbol_offset: int = 0,
        symbol_limit: int = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._symbols = symbols
        self._etf_symbols = etf_symbols or (source_config or {}).get("etf_symbols") or []
        self._since_date = since_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        self._symbol_limit = int(symbol_limit) if symbol_limit is not None else None
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
        to_date = date.today()
        fetched_at = utc_now_iso()

        symbols = self._symbols if self._symbols is not None else self._bw.read_universe()
        symbols = sorted({str(symbol).upper() for symbol in symbols if symbol})
        total_symbols = len(symbols)
        if self._symbols is None and (self._symbol_offset or self._symbol_limit is not None):
            end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
            symbols = symbols[self._symbol_offset:end]

        records = []
        for symbol in symbols:
            for function_name in _SYMBOL_FUNCTIONS:
                params = {"function": function_name, "symbol": symbol, "apikey": self._api_key}
                if function_name == "NEWS_SENTIMENT":
                    params.update({"tickers": symbol, "time_from": from_date.strftime("%Y%m%dT0000"), "limit": "1000"})
                    params.pop("symbol")
                records.append(self._fetch_record(function_name, fetched_at, params, symbol=symbol))

        for etf_symbol in sorted({str(symbol).upper() for symbol in self._etf_symbols if symbol}):
            records.append(self._fetch_record(
                "ETF_PROFILE",
                fetched_at,
                {"function": "ETF_PROFILE", "symbol": etf_symbol, "apikey": self._api_key},
                symbol=etf_symbol,
            ))

        records.append(self._fetch_record(
            "TREASURY_YIELD",
            fetched_at,
            {"function": "TREASURY_YIELD", "interval": "daily", "maturity": "3month", "apikey": self._api_key},
            maturity="3month",
        ))
        records.append(self._fetch_record(
            "CURRENCY_EXCHANGE_RATE",
            fetched_at,
            {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": "USD", "to_currency": "CHF", "apikey": self._api_key},
            ccy_pair="USDCHF",
        ))

        new_wm = Watermark(source_id=self.source_id, last_event_ts=to_date.isoformat(), last_cursor=to_date.isoformat())
        return Batch(
            records=records,
            new_wm=new_wm,
            window=self._window_id(from_date, to_date, symbols, total_symbols),
            partition_date=to_date.isoformat(),
        )

    def _fetch_record(self, function_name: str, fetched_at: str, params: dict, **context) -> dict:
        started_at = time.monotonic()
        resp = http_get(_AV_URL, params=params)
        payload = resp.json()
        self._raise_on_provider_message(function_name, payload)
        elapsed = time.monotonic() - started_at
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        return {
            "function": function_name,
            "context": context,
            "fetched_at": fetched_at,
            "payload": payload,
        }

    def _raise_on_provider_message(self, function_name: str, payload: dict) -> None:
        for key in ("Error Message", "Note", "Information"):
            if key in payload:
                raise RuntimeError(f"Alpha Vantage {function_name} returned {key}: {payload[key]}")

    def _window_id(self, from_date: date, to_date: date, symbols: list, total_symbols: int) -> str:
        symbol_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16] if symbols else "empty"
        etf_digest = hashlib.sha256("\n".join(sorted(self._etf_symbols)).encode("utf-8")).hexdigest()[:16] if self._etf_symbols else "no-etf"
        return (
            f"{from_date}-to-{to_date}"
            f"-symbols-{len(symbols)}-of-{total_symbols}"
            f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-{symbol_digest}"
            f"-etf-{etf_digest}"
        )
