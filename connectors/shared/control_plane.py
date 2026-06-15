"""Cosmos DB control-plane helpers — watermarks, run log, dedup, source registry."""
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

from .models import RunResult, Watermark


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CosmosControlPlane:
    def __init__(self, endpoint: str, database: str = "auspex") -> None:
        self._db = CosmosClient(endpoint, DefaultAzureCredential()).get_database_client(database)

    def _container(self, name: str):
        return self._db.get_container_client(name)

    # ------------------------------------------------------------------
    # Source registry
    # ------------------------------------------------------------------

    def get_source(self, source_id: str) -> Optional[dict]:
        try:
            return self._container("sources").read_item(item=source_id, partition_key=source_id)
        except CosmosResourceNotFoundError:
            return None

    def upsert_source(self, doc: dict) -> None:
        self._container("sources").upsert_item(doc)

    # ------------------------------------------------------------------
    # Watermarks
    # ------------------------------------------------------------------

    def read_watermark(self, source_id: str) -> Optional[Watermark]:
        try:
            item = self._container("watermarks").read_item(item=source_id, partition_key=source_id)
            return Watermark(
                source_id=item["source_id"],
                last_event_ts=item.get("last_event_ts"),
                last_cursor=item.get("last_cursor"),
                updated_at=item.get("updated_at"),
            )
        except CosmosResourceNotFoundError:
            return None

    def advance_watermark(
        self,
        source_id: str,
        run_id: str,
        last_event_ts: Optional[str] = None,
        last_cursor: Optional[str] = None,
    ) -> None:
        self._container("watermarks").upsert_item(
            {
                "id": source_id,
                "source_id": source_id,
                "last_event_ts": last_event_ts,
                "last_cursor": last_cursor,
                "updated_at": _now_utc(),
                "run_id": run_id,
            }
        )

    # ------------------------------------------------------------------
    # Run log
    # ------------------------------------------------------------------

    def start_run(self, run_id: str, source_id: str) -> None:
        self._container("runs").create_item(
            {
                "id": run_id,
                "source_id": source_id,
                "started_at": _now_utc(),
                "ended_at": None,
                "status": "running",
                "records_in": 0,
                "bytes": 0,
                "error": None,
            }
        )

    def end_run(self, run_id: str, source_id: str, result: RunResult) -> None:
        self._container("runs").patch_item(
            item=run_id,
            partition_key=source_id,
            patch_operations=[
                {"op": "set", "path": "/ended_at", "value": _now_utc()},
                {"op": "set", "path": "/status", "value": result.status},
                {"op": "set", "path": "/records_in", "value": result.records_in},
                {"op": "set", "path": "/bytes", "value": result.bytes_written},
                {"op": "set", "path": "/error", "value": result.error},
            ],
        )

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    def check_dedup(self, key: str, source_id: str) -> bool:
        doc_id = f"{source_id}:{key}"
        try:
            self._container("dedup").read_item(item=doc_id, partition_key=source_id)
            return True
        except CosmosResourceNotFoundError:
            return False

    def mark_dedup(self, key: str, source_id: str) -> None:
        doc_id = f"{source_id}:{key}"
        # ttl omitted — inherits container-level default (7 days)
        self._container("dedup").upsert_item(
            {
                "id": doc_id,
                "source_id": source_id,
                "key": key,
                "first_seen_at": _now_utc(),
            }
        )
