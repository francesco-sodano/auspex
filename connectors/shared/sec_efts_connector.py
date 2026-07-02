"""Generic SEC EFTS search connector for filing feeds."""
import os
import time
from datetime import date, timedelta
from typing import Optional

from .base_connector import BaseConnector
from .models import Batch, Watermark
from .retry import http_get

_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_PAGE_SIZE = 100
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_REQUESTS_PER_MINUTE = 450


class SecEftsConnector(BaseConnector):
    source_id: str
    schema_version = 1
    forms: str

    def __init__(self, cp, bw, source_config: Optional[dict] = None, since_date: str = None) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._user_agent = os.environ["EDGAR_USER_AGENT"]
        self._since_date = since_date
        self._min_interval_s = 60 / self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)

    def fetch(self, since: Optional[Watermark]) -> Batch:
        start_date = (
            self._since_date
            or (since.last_cursor if since and since.last_cursor else (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat())
        )
        end_date = date.today().isoformat()
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        records = []
        offset = 0

        while True:
            started_at = time.monotonic()
            resp = http_get(
                _EFTS_URL,
                params={
                    "forms": self.forms,
                    "startdt": start_date,
                    "enddt": end_date,
                    "from": offset,
                    "size": _PAGE_SIZE,
                },
                headers=headers,
            )
            elapsed = time.monotonic() - started_at
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)
            hits = resp.json().get("hits", {}).get("hits", [])
            for hit in hits:
                source = hit.get("_source", {})
                source["matched_forms"] = self.forms
                records.append(source)
            if len(hits) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        new_wm = Watermark(source_id=self.source_id, last_event_ts=end_date, last_cursor=end_date)
        return Batch(records=records, new_wm=new_wm, window=f"{start_date}-to-{end_date}-forms-{self.forms}", partition_date=end_date)
