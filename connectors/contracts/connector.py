"""USASpending.gov contract awards connector."""
from datetime import date, timedelta
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_post

_USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_DEFAULT_LOOKBACK_DAYS = 30
_DEFAULT_PAGE_LIMIT = 100
_CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]
_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Start Date",
    "End Date",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Description",
]


class ContractsConnector(BaseConnector):
    source_id = "contracts"
    schema_version = 1

    def __init__(
        self,
        cp,
        bw,
        search_terms: list = None,
        since_date: str = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._search_terms = search_terms if search_terms is not None else (source_config or {}).get("search_terms") or []
        self._since_date = since_date

    def fetch(self, since: Optional[Watermark]) -> Batch:
        start_date = (
            self._since_date
            or (since.last_cursor if since and since.last_cursor else (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat())
        )
        end_date = date.today().isoformat()
        records = []

        for term in self._normalized_terms():
            page = 1
            while True:
                payload = self._payload(term, start_date, end_date, page)
                data = http_post(_USASPENDING_URL, json=payload).json()
                results = data.get("results") or []
                for result in results:
                    records.append({"symbol": term.get("symbol"), "search_text": term["text"], "award": result})
                page_metadata = data.get("page_metadata") or {}
                if not page_metadata.get("hasNext"):
                    break
                page += 1

        new_wm = Watermark(source_id=self.source_id, last_event_ts=end_date, last_cursor=end_date)
        return Batch(records=records, new_wm=new_wm, window=f"{start_date}-to-{end_date}-terms-{len(self._normalized_terms())}", partition_date=end_date)

    def _normalized_terms(self) -> list[dict]:
        terms = []
        for term in self._search_terms:
            if isinstance(term, dict):
                text = term.get("text") or term.get("keyword") or term.get("recipient") or term.get("name")
                symbol = term.get("symbol")
            else:
                text = str(term)
                symbol = None
            if text:
                terms.append({"symbol": symbol.upper() if symbol else None, "text": text})
        return terms

    def _payload(self, term: dict, start_date: str, end_date: str, page: int) -> dict:
        return {
            "filters": {
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "award_type_codes": _CONTRACT_AWARD_TYPES,
                "keywords": [term["text"]],
            },
            "fields": _FIELDS,
            "page": page,
            "limit": _DEFAULT_PAGE_LIMIT,
            "sort": "Award Amount",
            "order": "desc",
        }
