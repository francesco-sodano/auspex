"""USASpending.gov contract awards connector."""
import hashlib
import json
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get, http_post

_USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
_USASPENDING_AWARD_URL = "https://api.usaspending.gov/api/v2/awards/{award_id}/"
_DEFAULT_LOOKBACK_DAYS = 30
_DEFAULT_PAGE_LIMIT = 100
_DETAIL_MAX_ATTEMPTS = 3
_DETAIL_TIMEOUT_SECONDS = 30.0
_CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]
_FIELDS = [
    "Award ID",
    "internal_id",
    "generated_internal_id",
    "Mod",
    "Action Date",
    "Recipient Name",
    "Recipient UEI",
    "recipient_id",
    "Transaction Amount",
    "Transaction Description",
    "Awarding Agency",
    "Awarding Sub Agency",
]


class ContractsConnector(BaseConnector):
    source_id = "contracts"
    schema_version = 2

    def __init__(
        self,
        cp,
        bw,
        search_terms: list = None,
        since_date: str = None,
        to_date: str = None,
        source_config: Optional[dict] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._search_terms = search_terms if search_terms is not None else (source_config or {}).get("search_terms") or []
        self._since_date = since_date
        self._to_date = to_date

    def fetch(self, since: Optional[Watermark]) -> Batch:
        start_date = (
            self._since_date
            or (since.last_cursor if since and since.last_cursor else (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat())
        )
        end_date = self._to_date or date.today().isoformat()
        records = []
        terms = self._normalized_terms()
        details_by_generated_award_id = {}

        for term in terms:
            page = 1
            while True:
                payload = self._payload(term, start_date, end_date, page)
                data = http_post(_USASPENDING_URL, json=payload).json()
                results = data.get("results") or []
                for result in results:
                    award_id = result.get("Award ID")
                    generated_award_id = result.get("generated_internal_id")
                    transaction_internal_id = result.get("internal_id")
                    action_date = result.get("Action Date")
                    if not award_id or not generated_award_id:
                        raise ValueError("USASpending search result is missing Award ID")
                    if transaction_internal_id is None or not action_date:
                        raise ValueError(f"USASpending transaction for {award_id} is missing identity or Action Date")
                    if generated_award_id not in details_by_generated_award_id:
                        details_by_generated_award_id[generated_award_id] = self._fetch_award_detail(result)
                    records.append(self._enriched_record(
                        term["text"], result, details_by_generated_award_id[generated_award_id],
                    ))
                page_metadata = data.get("page_metadata") or {}
                if not page_metadata.get("hasNext"):
                    break
                page += 1

        new_wm = Watermark(source_id=self.source_id, last_event_ts=end_date, last_cursor=end_date)
        terms_digest = hashlib.sha256(
            json.dumps(terms, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return Batch(
            records=records,
            new_wm=new_wm,
            window=f"{start_date}-to-{end_date}-terms-{len(terms)}-{terms_digest}",
            partition_date=end_date,
            watermark_from=start_date,
        )

    def _normalized_terms(self) -> list[dict]:
        terms = []
        for term in self._search_terms:
            if isinstance(term, dict):
                text = term.get("text") or term.get("keyword") or term.get("recipient") or term.get("name")
            else:
                text = str(term)
            if text:
                terms.append({"text": text})
        return terms

    def _fetch_award_detail(self, search_transaction: dict) -> dict:
        detail_key = search_transaction.get("generated_internal_id")
        if detail_key is None:
            raise ValueError(f"USASpending award {search_transaction['Award ID']} is missing a detail identifier")
        url = _USASPENDING_AWARD_URL.format(award_id=quote(str(detail_key), safe=""))
        return http_get(
            url,
            max_attempts=_DETAIL_MAX_ATTEMPTS,
            timeout=_DETAIL_TIMEOUT_SECONDS,
        ).json()

    @staticmethod
    def _enriched_record(search_text: str, search_transaction: dict, award_detail: dict) -> dict:
        recipient = award_detail.get("recipient") or {}
        generated_award_id = str(search_transaction["generated_internal_id"])
        transaction_internal_id = str(search_transaction["internal_id"])
        modification_number = str(search_transaction.get("Mod") or "")
        action_date = str(search_transaction["Action Date"])
        transaction_id = hashlib.sha256(transaction_internal_id.encode("utf-8")).hexdigest()
        record = {
            "search_text": search_text,
            "transaction_id": transaction_id,
            "transaction_internal_id": transaction_internal_id,
            "generated_award_id": generated_award_id,
            "award_id": search_transaction.get("Award ID"),
            "modification_number": modification_number,
            "action_date": action_date,
            "transaction_amount": search_transaction.get("Transaction Amount"),
            "transaction_description": search_transaction.get("Transaction Description"),
            "legal_recipient_name": recipient.get("recipient_name") or search_transaction.get("Recipient Name"),
            "recipient_id": recipient.get("recipient_hash") or search_transaction.get("recipient_id"),
            "recipient_uei": recipient.get("recipient_uei") or search_transaction.get("Recipient UEI"),
            "recipient_duns": recipient.get("recipient_unique_id"),
            "parent_recipient_name": recipient.get("parent_recipient_name"),
            "parent_recipient_id": recipient.get("parent_recipient_hash"),
            "parent_recipient_uei": recipient.get("parent_recipient_uei"),
            "parent_recipient_duns": recipient.get("parent_recipient_unique_id"),
            "search_transaction": search_transaction,
            "award_detail": award_detail,
        }
        recipient_cik = recipient.get("recipient_cik")
        if recipient_cik is None:
            recipient_cik = search_transaction.get("Recipient CIK") or search_transaction.get("recipient_cik")
        if recipient_cik is not None:
            record["recipient_cik"] = recipient_cik
        return record

    def _payload(self, term: dict, start_date: str, end_date: str, page: int) -> dict:
        return {
            "filters": {
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "award_type_codes": _CONTRACT_AWARD_TYPES,
                "recipient_search_text": [term["text"]],
            },
            "fields": _FIELDS,
            "page": page,
            "limit": _DEFAULT_PAGE_LIMIT,
            "sort": "Transaction Amount",
            "order": "desc",
        }
