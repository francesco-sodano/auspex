"""Alpha Vantage ETF profile/holdings connector for theme ground-truth seeds."""
import os
import time
from datetime import date
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

from alpha_vantage.mapping import utc_now_iso

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_REQUESTS_PER_MINUTE = 5


class EtfHoldingsConnector(BaseConnector):
    source_id = "etf_holdings"
    schema_version = 1

    def __init__(self, cp, bw, etf_symbols: list = None, source_config: Optional[dict] = None) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._etf_symbols = etf_symbols if etf_symbols is not None else (source_config or {}).get("etf_symbols") or []
        env_rpm = os.environ.get("AV_RPM")
        rpm = int(env_rpm) if env_rpm else self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        self._min_interval_s = 60 / rpm

    def fetch(self, since: Optional[Watermark]) -> Batch:
        today = date.today().isoformat()
        fetched_at = utc_now_iso()
        records = []
        for symbol in sorted({str(symbol).upper() for symbol in self._etf_symbols if symbol}):
            started_at = time.monotonic()
            payload = http_get(_AV_URL, params={"function": "ETF_PROFILE", "symbol": symbol, "apikey": self._api_key}).json()
            if "Error Message" in payload or "Note" in payload or "Information" in payload:
                message = payload.get("Error Message") or payload.get("Note") or payload.get("Information")
                raise RuntimeError(f"Alpha Vantage ETF_PROFILE returned: {message}")
            elapsed = time.monotonic() - started_at
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)
            records.append({"function": "ETF_PROFILE", "context": {"symbol": symbol}, "fetched_at": fetched_at, "payload": payload})

        new_wm = Watermark(source_id=self.source_id, last_event_ts=today, last_cursor=today)
        return Batch(records=records, new_wm=new_wm, window=f"{today}-etfs-{len(records)}", partition_date=today)
