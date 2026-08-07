"""SEC EDGAR Company Facts connector for Alpha Vantage coverage symbols."""
import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get
from shared.sec_efts_connector import _pace_sec_request

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_REQUESTS_PER_MINUTE = 60
_SEC_MAX_ATTEMPTS = 6
_SEC_TIMEOUT_SECONDS = 60.0
_SEC_MAX_REQUESTS_PER_MINUTE = 60


class SecCompanyFactsConnector(BaseConnector):
    source_id = "sec_companyfacts"
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
        self._user_agent = os.environ["EDGAR_USER_AGENT"]
        self._symbols = symbols
        self._since_date = since_date
        self._to_date = to_date
        self._symbol_offset = max(0, int(symbol_offset or 0))
        self._symbol_limit = max(1, int(symbol_limit)) if symbol_limit is not None else None
        configured_rpm = self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        max_rpm = int(os.environ.get("SEC_EFTS_MAX_RPM", str(_SEC_MAX_REQUESTS_PER_MINUTE)))
        self._min_interval_s = 60 / min(configured_rpm, max_rpm)
        self._before_sec_request = lambda: _pace_sec_request(self._min_interval_s)

    def fetch(self, since: Optional[Watermark]) -> Batch:
        from_date, to_date = self._date_window(since)
        symbols = self._selected_symbols()
        total_symbols = len(symbols)
        has_more = False
        if self._symbols is None and (self._symbol_offset or self._symbol_limit is not None):
            end = None if self._symbol_limit is None else self._symbol_offset + self._symbol_limit
            symbols = symbols[self._symbol_offset:end]
            has_more = self._symbol_offset + len(symbols) < total_symbols

        window = self._window_id(from_date, to_date, symbols, total_symbols)
        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=to_date.isoformat(),
            last_cursor=to_date.isoformat(),
        )
        if from_date > to_date or not symbols:
            return Batch(
                records=[],
                new_wm=new_wm,
                window=window,
                partition_date=to_date.isoformat(),
                watermark_from=from_date.isoformat(),
                has_more=has_more,
            )

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        ticker_to_cik = self._fetch_ticker_to_cik(headers)
        fetched_at = datetime.now(timezone.utc).isoformat()
        records = []
        for symbol in symbols:
            cik = ticker_to_cik.get(symbol)
            if cik is None:
                records.append({
                    "fetched_at": fetched_at,
                    "context": {"symbol": symbol, "cik": None},
                    "status": "missing_cik",
                    "payload": None,
                })
                continue

            try:
                response = http_get(
                    _COMPANY_FACTS_URL.format(cik=cik),
                    headers=headers,
                    max_attempts=_SEC_MAX_ATTEMPTS,
                    timeout=_SEC_TIMEOUT_SECONDS,
                    before_attempt=self._before_sec_request,
                )
            except httpx.HTTPStatusError as exc:
                if getattr(exc.response, "status_code", None) != 404:
                    raise
                records.append({
                    "fetched_at": fetched_at,
                    "context": {"symbol": symbol, "cik": cik},
                    "status": "missing_companyfacts",
                    "payload": None,
                })
                continue
            records.append({
                "fetched_at": fetched_at,
                "context": {"symbol": symbol, "cik": cik},
                "status": "ok",
                "payload": response.json(),
            })

        return Batch(
            records=records,
            new_wm=new_wm,
            window=window,
            partition_date=to_date.isoformat(),
            watermark_from=from_date.isoformat(),
            has_more=has_more,
        )

    def _date_window(self, since: Optional[Watermark]) -> tuple[date, date]:
        if self._since_date:
            from_date = date.fromisoformat(self._since_date)
        elif since and since.last_event_ts:
            from_date = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            from_date = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        to_date = date.fromisoformat(self._to_date) if self._to_date else date.today()
        return from_date, to_date

    def _selected_symbols(self) -> list[str]:
        symbols = (
            self._symbols
            if self._symbols is not None
            else self._bw.read_universe("alpha_vantage", "coverage")
        )
        return sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})

    def _fetch_ticker_to_cik(self, headers: dict) -> dict[str, str]:
        response = http_get(
            _COMPANY_TICKERS_URL,
            headers=headers,
            max_attempts=_SEC_MAX_ATTEMPTS,
            timeout=_SEC_TIMEOUT_SECONDS,
            before_attempt=self._before_sec_request,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SEC company tickers payload must be an object")

        ticker_to_cik = {}
        for entry in payload.values():
            if not isinstance(entry, dict) or not entry.get("ticker") or entry.get("cik_str") is None:
                continue
            try:
                cik = f"{int(entry['cik_str']):010d}"
            except (TypeError, ValueError):
                continue
            ticker_to_cik[str(entry["ticker"]).strip().upper()] = cik
        return ticker_to_cik

    def _window_id(self, from_date: date, to_date: date, symbols: list[str], total_symbols: int) -> str:
        symbol_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()[:16] if symbols else "empty"
        return (
            f"{from_date}-to-{to_date}"
            f"-symbols-{len(symbols)}-of-{total_symbols}"
            f"-offset-{self._symbol_offset}-limit-{self._symbol_limit or 'all'}-{symbol_digest}"
        )