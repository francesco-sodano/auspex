"""Cosmos DB control-plane helpers — watermarks, run log, dedup, source registry."""
from datetime import datetime, timezone
import hashlib
from itertools import islice
from typing import Optional

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

from .models import RunResult, Watermark
from engine.company_package import CompanyOpportunityPackage, package_document


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

    def get_security_by_ticker(self, ticker: str) -> Optional[dict]:
        try:
            normalized = str(ticker or "").strip().upper()
            return self._container("security_catalog").read_item(
                item=f"ticker:{normalized}",
                partition_key=f"ticker:{normalized}",
            )
        except CosmosResourceNotFoundError:
            return None

    def get_market_data(self, document_id: str) -> Optional[dict]:
        try:
            return self._container("market_data").read_item(
                item=document_id,
                partition_key=document_id,
            )
        except CosmosResourceNotFoundError:
            return None

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
    # Incremental company package changes
    # ------------------------------------------------------------------

    def mark_company_dirty(
        self,
        *,
        security_sk: int,
        source_class: str,
        source_id: str,
        source_record_id: str,
        revision_hash: str,
        knowledge_date: str,
    ) -> str:
        if security_sk <= 0:
            raise ValueError("security_sk must be positive")
        identity_parts = (
            str(security_sk),
            source_class.strip(),
            source_id.strip(),
            source_record_id.strip(),
            revision_hash.strip(),
            knowledge_date.strip(),
        )
        if any(not value for value in identity_parts):
            raise ValueError("dirty company event identity is incomplete")
        datetime.fromisoformat(knowledge_date.replace("Z", "+00:00"))
        digest = hashlib.sha256("|".join(identity_parts).encode("utf-8")).hexdigest()
        event_id = f"dirty:{digest}"
        try:
            self._container("dirty_company_events").create_item({
                "id": event_id,
                "security_sk": security_sk,
                "source_class": source_class,
                "source_id": source_id,
                "source_record_id": source_record_id,
                "revision_hash": revision_hash,
                "knowledge_date": knowledge_date,
                "status": "pending",
                "created_at": _now_utc(),
                "processed_at": None,
                "package_fingerprint": None,
            })
        except CosmosResourceExistsError:
            pass
        return event_id

    def list_pending_company_changes(self, limit: int = 1000) -> list[dict]:
        if limit < 1 or limit > 10000:
            raise ValueError("dirty company event limit must be between 1 and 10000")
        documents = self._container("dirty_company_events").query_items(
            query=(
                "SELECT * FROM c WHERE c.status = 'pending' "
                "ORDER BY c.knowledge_date, c.id"
            ),
            enable_cross_partition_query=True,
        )
        return list(islice(documents, limit))

    def complete_company_changes(
        self,
        *,
        security_sk: int,
        change_ids: list[str],
        package_fingerprint: str,
    ) -> None:
        if security_sk <= 0 or not package_fingerprint.strip():
            raise ValueError("company package completion identity is incomplete")
        if not change_ids or len(change_ids) != len(set(change_ids)):
            raise ValueError("company package completion requires unique change ids")
        processed_at = _now_utc()
        container = self._container("dirty_company_events")
        for change_id in change_ids:
            container.patch_item(
                item=change_id,
                partition_key=security_sk,
                patch_operations=[
                    {"op": "set", "path": "/status", "value": "processed"},
                    {"op": "set", "path": "/processed_at", "value": processed_at},
                    {
                        "op": "set",
                        "path": "/package_fingerprint",
                        "value": package_fingerprint,
                    },
                ],
            )

    def publish_company_package(self, package: CompanyOpportunityPackage) -> str:
        revision = package_document(package)
        container = self._container("company_packages")
        try:
            container.create_item(revision)
        except CosmosResourceExistsError:
            existing = container.read_item(
                item=revision["id"],
                partition_key=package.security_sk,
            )
            if any(existing.get(key) != value for key, value in revision.items()):
                raise RuntimeError("company package revision identity has conflicting content")
        current = {
            **revision,
            "id": "current",
            "document_type": "current",
            "revision_id": revision["id"],
        }
        container.upsert_item(current)
        return revision["package_fingerprint"]

    def get_current_company_package(self, security_sk: int) -> Optional[dict]:
        try:
            return self._container("company_packages").read_item(
                item="current",
                partition_key=security_sk,
            )
        except CosmosResourceNotFoundError:
            return None

    def list_current_company_packages(self) -> list[dict]:
        return list(self._container("company_packages").query_items(
            query=(
                "SELECT * FROM c WHERE c.id = 'current' "
                "AND c.document_type = 'current'"
            ),
            enable_cross_partition_query=True,
        ))

    def attach_company_narrative(
        self,
        *,
        security_sk: int,
        package_fingerprint: str,
        narrative: dict,
    ) -> None:
        current = self.get_current_company_package(security_sk)
        if current is None or current.get("package_fingerprint") != package_fingerprint:
            raise RuntimeError("company narrative package identity is stale")
        self._container("company_packages").patch_item(
            item="current",
            partition_key=security_sk,
            patch_operations=[
                {"op": "set", "path": "/narrative", "value": narrative},
            ],
        )

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

    def start_run(self, run_id: str, source_id: str) -> Optional[RunResult]:
        container = self._container("runs")
        try:
            container.create_item(
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
            return None
        except CosmosResourceExistsError:
            existing = container.read_item(item=run_id, partition_key=source_id)
            replay_fields = {"has_more", "last_event_ts", "last_cursor"}
            if existing.get("ended_at") is None or not replay_fields.issubset(existing):
                return None
            return RunResult(
                status=existing["status"],
                records_in=int(existing.get("records_in") or 0),
                bytes_written=int(existing.get("bytes") or 0),
                error=existing.get("error"),
                has_more=existing.get("has_more"),
                last_event_ts=existing.get("last_event_ts"),
                last_cursor=existing.get("last_cursor"),
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
                {"op": "set", "path": "/has_more", "value": result.has_more},
                {"op": "set", "path": "/last_event_ts", "value": result.last_event_ts},
                {"op": "set", "path": "/last_cursor", "value": result.last_cursor},
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
