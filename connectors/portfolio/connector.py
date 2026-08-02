"""Immutable portfolio-ledger snapshot connector for Fabric Bronze."""
import hashlib
import json
from datetime import date
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark


_EXCLUDED_FIELDS = {"_attachments", "_etag", "_rid", "_self", "_ts", "id", "schema_version"}


class PortfolioConnector(BaseConnector):
    source_id = "portfolio"
    schema_version = 5

    def fetch(self, since: Optional[Watermark]) -> Batch:
        transactions = [
            {
                key: value
                for key, value in document.items()
                if key not in _EXCLUDED_FIELDS
            }
            for document in self._cp.list_portfolio_transactions()
        ]
        transactions.sort(key=lambda record: (record["created_at"], record["transaction_id"]))
        digest = hashlib.sha256(
            json.dumps(transactions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        partition_date = max(
            (str(record["created_at"])[:10] for record in transactions),
            default=date.today().isoformat(),
        )
        records = [{
            "record_type": "snapshot_manifest",
            "snapshot_id": digest,
            "snapshot_date": partition_date,
            "transaction_count": len(transactions),
        }]
        records.extend({
            **transaction,
            "record_type": "transaction",
            "snapshot_id": digest,
        } for transaction in transactions)
        return Batch(
            records=records,
            new_wm=Watermark(
                source_id=self.source_id,
                last_event_ts=partition_date,
                last_cursor=digest,
            ),
            window=f"snapshot-{digest}",
            partition_date=partition_date,
            watermark_from=since.last_event_ts if since else None,
        )
