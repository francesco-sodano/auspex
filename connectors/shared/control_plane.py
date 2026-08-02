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

    def container(self, name: str):
        return self._container(name)

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

    def upsert_market_data(self, document: dict) -> dict:
        container = self._container("market_data")
        try:
            current = container.read_item(
                item=document["id"],
                partition_key=document["id"],
            )
        except CosmosResourceNotFoundError:
            current = None
        if current and current.get("as_of", "") > document.get("as_of", ""):
            if document.get("source_id") == "fabric" and document.get("generation"):
                preserved = dict(current)
                preserved["source_id"] = "fabric"
                preserved["generation"] = document["generation"]
                return container.upsert_item(preserved)
            return current
        return container.upsert_item(document)

    def upsert_security_catalog(self, document: dict) -> None:
        self._container("security_catalog").upsert_item(document)

    def count_documents(self, container_name: str) -> int:
        values = self._container(container_name).query_items(
            query="SELECT VALUE COUNT(1) FROM c",
            enable_cross_partition_query=True,
        )
        return int(next(iter(values), 0))

    def delete_stale_projection_generation(
        self,
        container_name: str,
        generation: str,
    ) -> int:
        container = self._container(container_name)
        documents = list(container.query_items(
            query=(
                "SELECT c.id FROM c WHERE c.source_id = 'fabric' "
                "AND (NOT IS_DEFINED(c.generation) OR c.generation != @generation)"
            ),
            parameters=[{"name": "@generation", "value": generation}],
            enable_cross_partition_query=True,
        ))
        for document in documents:
            container.delete_item(item=document["id"], partition_key=document["id"])
        return len(documents)

    def list_portfolio_transactions(self) -> list[dict]:
        return list(self._container("portfolio_transactions").query_items(
            query="SELECT * FROM c WHERE c.id != '_ledger_revision'",
            enable_cross_partition_query=True,
        ))

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
        self._container("dedup").upsert_item(
            {
                "id": doc_id,
                "source_id": source_id,
                "key": key,
                "first_seen_at": _now_utc(),
                "ttl": -1,
            }
        )
