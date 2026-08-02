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
_FX_PAIRS = ("USDCHF", "USDEUR", "USDGBP")
_PROFILES = {
    "combined": {
        "symbol_functions": _SYMBOL_FUNCTIONS,
        "universe_name": "prices",
        "universe_tier": None,
        "include_etfs": True,
        "include_global": True,
    },
    "news_daily": {
        "symbol_functions": ["NEWS_SENTIMENT"],
        "universe_name": "alpha_vantage",
        "universe_tier": "active",
        "include_etfs": False,
        "include_global": False,
    },
    "macro_daily": {
        "symbol_functions": [],
        "universe_name": None,
        "universe_tier": None,
        "include_etfs": False,
        "include_global": True,
    },
    "themes_weekly": {
        "symbol_functions": [],
        "universe_name": None,
        "universe_tier": None,
        "include_etfs": True,
        "include_global": False,
    },
    "fundamentals_quarterly": {
        "symbol_functions": ["OVERVIEW", "BALANCE_SHEET", "CASH_FLOW"],
        "universe_name": "alpha_vantage",
        "universe_tier": "coverage",
        "include_etfs": False,
        "include_global": False,
    },
    "holdings_quarterly": {
        "symbol_functions": ["INSTITUTIONAL_HOLDINGS"],
        "universe_name": "alpha_vantage",
        "universe_tier": "coverage",
        "include_etfs": False,
        "include_global": False,
    },
}


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
        include_etfs: Optional[bool] = None,
        include_global: Optional[bool] = None,
        profile: str = "combined",
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        if profile not in _PROFILES:
            raise ValueError(f"Unsupported Alpha Vantage profile: {profile}")
        profile_config = _PROFILES[profile]
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._profile = profile
        self._symbol_functions = profile_config["symbol_functions"]
        self._universe_name = profile_config["universe_name"]
        self._universe_tier = profile_config["universe_tier"]
        self._symbols = symbols
        self._etf_symbols = etf_symbols or (source_config or {}).get("etf_symbols") or []
        self._since_date = since_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        configured_limit = ((source_config or {}).get("profiles") or {}).get(profile, {}).get("symbol_limit")
        effective_limit = symbol_limit if symbol_limit is not None else configured_limit
        self._symbol_limit = int(effective_limit) if effective_limit is not None else None
        self._include_etfs = profile_config["include_etfs"] if include_etfs is None else bool(include_etfs)
        self._include_global = profile_config["include_global"] if include_global is None else bool(include_global)
        env_rpm = os.environ.get("AV_RPM")
        self._requests_per_minute_value = int(env_rpm) if env_rpm else self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        self._min_interval_s = 60 / self._requests_per_minute_value

    @property
    def watermark_source_id(self) -> str:
        if self._profile == "combined":
            return self.source_id
        return f"{self.source_id}:{self._profile}"

    def fetch(self, since: Optional[Watermark]) -> Batch:
        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        to_date = date.today()
        fetched_at = utc_now_iso()

        if not self._symbol_functions:
            symbols = []
        elif self._symbols is not None:
            symbols = self._symbols
        else:
            symbols = self._bw.read_universe(self._universe_name, self._universe_tier)
        symbols = sorted({str(symbol).upper() for symbol in symbols if symbol})
        if self._symbol_functions and not symbols and self._profile != "combined":
            raise RuntimeError(f"Alpha Vantage {self._universe_tier} universe is empty")
        total_symbols = len(symbols)
        has_more = False
        if self._symbols is None and (self._symbol_offset or self._symbol_limit is not None):
            end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
            symbols = symbols[self._symbol_offset:end]
            has_more = self._symbol_offset + len(symbols) < total_symbols

        records = []
        for symbol in symbols:
            for function_name in self._symbol_functions:
                params = {"function": function_name, "symbol": symbol, "apikey": self._api_key}
                if function_name == "NEWS_SENTIMENT":
                    params.update({"tickers": symbol, "time_from": from_date.strftime("%Y%m%dT0000"), "limit": "1000"})
                    params.pop("symbol")
                records.append(self._fetch_record(function_name, fetched_at, params, symbol=symbol))

        if self._include_etfs:
            for etf_symbol in sorted({str(symbol).upper() for symbol in self._etf_symbols if symbol}):
                records.append(self._fetch_record(
                    "ETF_PROFILE",
                    fetched_at,
                    {"function": "ETF_PROFILE", "symbol": etf_symbol, "apikey": self._api_key},
                    symbol=etf_symbol,
                ))

        if self._include_global:
            records.append(self._fetch_record(
                "TREASURY_YIELD",
                fetched_at,
                {"function": "TREASURY_YIELD", "interval": "daily", "maturity": "3month", "apikey": self._api_key},
                maturity="3month",
            ))
            for ccy_pair in _FX_PAIRS:
                target_currency = ccy_pair[3:]
                records.append(self._fetch_record(
                    "CURRENCY_EXCHANGE_RATE",
                    fetched_at,
                    {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": "USD", "to_currency": target_currency, "apikey": self._api_key},
                    ccy_pair=ccy_pair,
                ))

        new_wm = Watermark(source_id=self.source_id, last_event_ts=to_date.isoformat(), last_cursor=to_date.isoformat())
        return Batch(
            records=records,
            new_wm=new_wm,
            window=self._window_id(from_date, to_date, symbols, total_symbols),
            partition_date=to_date.isoformat(),
            watermark_from=from_date.isoformat(),
            has_more=has_more,
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
            "profile": self._profile,
            "context": context,
            "fetched_at": fetched_at,
            "payload": payload,
        }

    def _raise_on_provider_message(self, function_name: str, payload: dict) -> None:
        for key in ("Error Message", "Note", "Information"):
            if key in payload:
                raise RuntimeError(f"Alpha Vantage {function_name} returned {key}: {payload[key]}")

    def after_bronze_write(self, batch: Batch) -> None:
        for record in batch.records:
            if record.get("function") != "CURRENCY_EXCHANGE_RATE":
                continue
            exchange_rate = (record.get("payload") or {}).get("Realtime Currency Exchange Rate") or {}
            rate = exchange_rate.get("5. Exchange Rate")
            if not rate:
                raise ValueError("Alpha Vantage FX projection is missing exchange rate")
            pair = str((record.get("context") or {}).get("ccy_pair") or "").upper()
            as_of = str(exchange_rate.get("6. Last Refreshed") or record["fetched_at"])[:10]
            self._cp.upsert_market_data({
                "id": f"fx:{pair}:{as_of}",
                "kind": "fx",
                "pair": pair,
                "rate": str(rate),
                "as_of": as_of,
                "source_id": self.source_id,
            })
            self._cp.upsert_market_data({
                "id": f"fx:{pair}",
                "kind": "fx_alias",
                "pair": pair,
                "rate": str(rate),
                "as_of": as_of,
                "source_id": self.source_id,
            })

    def _window_id(self, from_date: date, to_date: date, symbols: list, total_symbols: int) -> str:
        symbol_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16] if symbols else "empty"
        etf_digest = hashlib.sha256("\n".join(sorted(self._etf_symbols)).encode("utf-8")).hexdigest()[:16] if self._etf_symbols else "no-etf"
        return (
            f"{from_date}-to-{to_date}"
            f"-profile-{self._profile}"
            f"-symbols-{len(symbols)}-of-{total_symbols}"
            f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-{symbol_digest}"
            f"-etf-{etf_digest}"
            f"-include-etfs-{int(self._include_etfs)}"
            f"-include-global-{int(self._include_global)}"
        )
