"""SEC EDGAR Form 4 connector — insider transaction filings."""
import os
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_PAGE_SIZE = 100
_DEFAULT_LOOKBACK_DAYS = 7


class SecForm4Connector(BaseConnector):
    source_id = "sec_form4"
    schema_version = 1

    def __init__(self, cp, bw) -> None:
        super().__init__(cp, bw)
        self._user_agent = os.environ["EDGAR_USER_AGENT"]

    def fetch(self, since: Optional[Watermark]) -> Batch:
        start_date = (
            since.last_cursor
            if since and since.last_cursor
            else (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat()
        )
        end_date = date.today().isoformat()

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        records = []
        offset = 0

        while True:
            resp = http_get(
                _EFTS_URL,
                params={
                    "forms": "4",
                    "startdt": start_date,
                    "enddt": end_date,
                    "from": offset,
                    "size": _PAGE_SIZE,
                },
                headers=headers,
            )
            hits = resp.json()["hits"]["hits"]
            records.extend(h["_source"] for h in hits)
            if len(hits) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        new_wm = Watermark(
            source_id=self.source_id,
            last_event_ts=end_date,
            last_cursor=end_date,
        )
        return Batch(records=records, new_wm=new_wm, window=f"{start_date}-to-{end_date}")
