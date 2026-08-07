"""Destructively reset non-production Auspex state except owner profile and ledger."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import httpx
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient
from mssql_python import connect


CONFIRMATION_TOKEN = "DELETE-LEGACY-AUSPEX-ENGINE"
PRESERVED_COSMOS_CONTAINERS = {"app_users", "portfolio_transactions"}
PRESERVED_FABRIC_ITEM_TYPES = {"Lakehouse", "Warehouse"}
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"
SEARCH_API_VERSION = "2024-07-01"


@dataclass(frozen=True)
class WarehouseObject:
    schema_name: str
    object_name: str
    object_type: str


@dataclass(frozen=True)
class OneLakeObject:
    path: str
    is_directory: bool


@dataclass(frozen=True)
class ResetPlan:
    preserve_cosmos: tuple[str, ...]
    purge_cosmos: tuple[str, ...]
    delete_onelake_paths: tuple[OneLakeObject, ...]
    drop_warehouse_objects: tuple[WarehouseObject, ...]
    delete_search_indexes: tuple[str, ...]
    delete_fabric_items: tuple[dict, ...]


def build_reset_plan(
    *,
    cosmos_containers: list[str],
    onelake_paths: list[OneLakeObject],
    warehouse_objects: list[WarehouseObject],
    search_indexes: list[str],
    fabric_items: list[dict],
) -> ResetPlan:
    available = set(cosmos_containers)
    missing_preserved = PRESERVED_COSMOS_CONTAINERS - available
    if missing_preserved:
        raise RuntimeError(
            "required preserved Cosmos containers are missing: "
            + ", ".join(sorted(missing_preserved))
        )
    return ResetPlan(
        preserve_cosmos=tuple(sorted(PRESERVED_COSMOS_CONTAINERS)),
        purge_cosmos=tuple(sorted(available - PRESERVED_COSMOS_CONTAINERS)),
        delete_onelake_paths=tuple(sorted(
            onelake_paths,
            key=lambda item: (item.path, item.is_directory),
        )),
        drop_warehouse_objects=tuple(sorted(
            warehouse_objects,
            key=lambda row: (_drop_order(row.object_type), row.schema_name, row.object_name),
        )),
        delete_search_indexes=tuple(sorted(set(search_indexes))),
        delete_fabric_items=tuple(sorted(
            (
                {
                    "id": str(item["id"]),
                    "display_name": str(item["displayName"]),
                    "type": str(item["type"]),
                }
                for item in fabric_items
                if item.get("type") not in PRESERVED_FABRIC_ITEM_TYPES
            ),
            key=lambda item: (item["type"], item["display_name"], item["id"]),
        )),
    )


def preservation_manifest(documents_by_container: dict[str, list[dict]]) -> dict:
    if set(documents_by_container) != PRESERVED_COSMOS_CONTAINERS:
        raise ValueError("preservation export must contain exactly the approved containers")
    containers = {}
    for name in sorted(documents_by_container):
        documents = sorted(
            documents_by_container[name],
            key=lambda document: (str(document.get("id")), _canonical(document)),
        )
        if any(not str(document.get("id") or "").strip() for document in documents):
            raise ValueError(f"preserved container {name} contains a document without id")
        containers[name] = {
            "count": len(documents),
            "sha256": hashlib.sha256(_canonical(documents).encode("utf-8")).hexdigest(),
            "documents": documents,
        }
    payload = {"schema_version": 1, "containers": containers}
    payload["sha256"] = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return payload


def warehouse_drop_statements(objects: tuple[WarehouseObject, ...]) -> list[str]:
    statements = []
    prefixes = {"V": "VIEW", "P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION", "TF": "FUNCTION", "U": "TABLE"}
    for row in objects:
        prefix = prefixes.get(row.object_type)
        if prefix is None:
            raise ValueError(f"unsupported Warehouse object type: {row.object_type}")
        schema = row.schema_name.replace("]", "]]" )
        name = row.object_name.replace("]", "]]" )
        statements.append(f"DROP {prefix} [{schema}].[{name}]")
    return statements


def require_confirmation(apply: bool, confirmation: str) -> None:
    if apply and confirmation != CONFIRMATION_TOKEN:
        raise RuntimeError(
            f"destructive reset requires --confirmation {CONFIRMATION_TOKEN}"
        )


class LegacyEngineReset:
    def __init__(
        self,
        *,
        cosmos_endpoint: str,
        cosmos_database: str,
        workspace_id: str,
        lakehouse_id: str,
        warehouse_server: str,
        warehouse_database: str,
        search_endpoint: str,
        credential=None,
    ) -> None:
        self.credential = credential or DefaultAzureCredential()
        self.cosmos_database = CosmosClient(
            cosmos_endpoint, self.credential
        ).get_database_client(cosmos_database)
        self.workspace_id = workspace_id
        self.lakehouse_id = lakehouse_id
        self.file_system = DataLakeServiceClient(
            "https://onelake.dfs.fabric.microsoft.com", self.credential
        ).get_file_system_client(workspace_id)
        self.warehouse_server = warehouse_server
        self.warehouse_database = warehouse_database
        self.search_endpoint = search_endpoint.rstrip("/")
        self.http = httpx.Client(timeout=120)

    def inspect(self) -> tuple[ResetPlan, dict[str, list[dict]]]:
        cosmos_containers = [
            container["id"] for container in self.cosmos_database.list_containers()
        ]
        preserved = {
            name: list(self.cosmos_database.get_container_client(name).query_items(
                "SELECT * FROM c", enable_cross_partition_query=True
            ))
            for name in PRESERVED_COSMOS_CONTAINERS
        }
        onelake_paths: list[OneLakeObject] = []
        for root in (
            f"{self.lakehouse_id}/Files",
            f"{self.lakehouse_id}/Tables",
        ):
            onelake_paths.extend(
                OneLakeObject(path=path.name, is_directory=bool(path.is_directory))
                for path in self.file_system.get_paths(path=root)
            )
        warehouse_objects = self._warehouse_objects()
        search_indexes = [item["name"] for item in self._search("GET", "indexes").get("value", [])]
        fabric_items = self._fabric("GET", "items").get("value", [])
        return build_reset_plan(
            cosmos_containers=cosmos_containers,
            onelake_paths=onelake_paths,
            warehouse_objects=warehouse_objects,
            search_indexes=search_indexes,
            fabric_items=fabric_items,
        ), preserved

    def apply(self, plan: ResetPlan, export_path: Path, preserved: dict[str, list[dict]]) -> dict:
        manifest = preservation_manifest(preserved)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        written = json.loads(export_path.read_text(encoding="utf-8"))
        if written.get("sha256") != manifest["sha256"]:
            raise RuntimeError("preservation export verification failed")
        if preservation_manifest(self._read_preserved())["sha256"] != manifest["sha256"]:
            raise RuntimeError("preserved Cosmos state changed during reset preparation")

        for item in plan.delete_fabric_items:
            self._fabric("DELETE", f"items/{item['id']}")
        self._drop_warehouse(plan.drop_warehouse_objects)
        for index_name in plan.delete_search_indexes:
            self._search("DELETE", f"indexes/{index_name}")
        for container_name in plan.purge_cosmos:
            self._purge_cosmos_container(container_name)
        for item in sorted(
            plan.delete_onelake_paths,
            key=lambda value: value.path,
            reverse=True,
        ):
            self._delete_onelake_path(item)

        final_manifest = preservation_manifest(self._read_preserved())
        if final_manifest["sha256"] != manifest["sha256"]:
            raise RuntimeError("preserved Cosmos state changed during destructive reset")
        self.verify(plan)
        return {
            "status": "reset",
            "preservation_export": str(export_path),
            "preservation_sha256": manifest["sha256"],
            "purged_cosmos_containers": len(plan.purge_cosmos),
            "deleted_onelake_paths": len(plan.delete_onelake_paths),
            "dropped_warehouse_objects": len(plan.drop_warehouse_objects),
            "deleted_search_indexes": len(plan.delete_search_indexes),
            "deleted_fabric_items": len(plan.delete_fabric_items),
        }

    def verify(self, plan: ResetPlan) -> None:
        nonempty_containers = []
        for container_name in plan.purge_cosmos:
            values = self.cosmos_database.get_container_client(container_name).query_items(
                "SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True
            )
            if int(next(iter(values), 0)):
                nonempty_containers.append(container_name)
        if nonempty_containers:
            raise RuntimeError(
                "legacy Cosmos containers remain nonempty: "
                + ", ".join(nonempty_containers)
            )
        remaining_onelake = [
            item.path for item in plan.delete_onelake_paths if self._onelake_exists(item)
        ]
        if remaining_onelake:
            raise RuntimeError(
                "legacy OneLake paths remain: " + ", ".join(remaining_onelake[:20])
            )
        remaining_warehouse = self._warehouse_objects()
        if remaining_warehouse:
            raise RuntimeError("legacy Warehouse objects remain after reset")
        remaining_indexes = [
            item["name"] for item in self._search("GET", "indexes").get("value", [])
        ]
        if remaining_indexes:
            raise RuntimeError(
                "legacy Search indexes remain: " + ", ".join(remaining_indexes)
            )
        remaining_items = [
            item for item in self._fabric("GET", "items").get("value", [])
            if item.get("type") not in PRESERVED_FABRIC_ITEM_TYPES
        ]
        if remaining_items:
            raise RuntimeError("legacy Fabric compute items remain after reset")

    def _read_preserved(self) -> dict[str, list[dict]]:
        return {
            name: list(self.cosmos_database.get_container_client(name).query_items(
                "SELECT * FROM c", enable_cross_partition_query=True
            ))
            for name in PRESERVED_COSMOS_CONTAINERS
        }

    def _purge_cosmos_container(self, container_name: str) -> None:
        container = self.cosmos_database.get_container_client(container_name)
        properties = container.read()
        partition_paths = (properties.get("partitionKey") or {}).get("paths") or []
        if len(partition_paths) != 1:
            raise RuntimeError(f"container {container_name} must have one partition path")
        partition_path = partition_paths[0]
        documents = list(container.query_items(
            "SELECT * FROM c", enable_cross_partition_query=True
        ))
        for document in documents:
            container.delete_item(
                item=document["id"],
                partition_key=_path_value(document, partition_path),
            )

    def _warehouse_objects(self) -> list[WarehouseObject]:
        connection = self._warehouse_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT s.name, o.name, o.type
                FROM sys.objects o
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE o.is_ms_shipped = 0
                  AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
                  AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'U')
            """)
            return [WarehouseObject(*row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def _drop_warehouse(self, objects: tuple[WarehouseObject, ...]) -> None:
        connection = self._warehouse_connection()
        try:
            cursor = connection.cursor()
            for statement in warehouse_drop_statements(objects):
                cursor.execute(statement)
            cursor.execute("SELECT @@TRANCOUNT")
            if int(cursor.fetchone()[0]):
                raise RuntimeError("Warehouse reset left an open transaction")
        finally:
            connection.close()

    def _warehouse_connection(self):
        connection = connect(
            f"Server={self.warehouse_server};Database={self.warehouse_database};"
            "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
        )
        connection.autocommit = True
        return connection

    def _delete_onelake_path(self, item: OneLakeObject) -> None:
        if item.is_directory:
            self.file_system.get_directory_client(item.path).delete_directory()
        else:
            self.file_system.get_file_client(item.path).delete_file()

    def _onelake_exists(self, item: OneLakeObject) -> bool:
        try:
            if item.is_directory:
                self.file_system.get_directory_client(item.path).get_directory_properties()
            else:
                self.file_system.get_file_client(item.path).get_file_properties()
            return True
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return False
            raise

    def _fabric(self, method: str, path: str) -> dict:
        response = self.http.request(
            method,
            f"https://api.fabric.microsoft.com/v1/workspaces/{self.workspace_id}/{path}",
            headers={
                "Authorization": f"Bearer {self.credential.get_token(FABRIC_SCOPE).token}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Fabric {method} {path} failed: {response.text[:1000]}")
        self._wait_for_operation(response, FABRIC_SCOPE)
        return response.json() if response.content else {}

    def _search(self, method: str, path: str) -> dict:
        response = self.http.request(
            method,
            f"{self.search_endpoint}/{path}",
            params={"api-version": SEARCH_API_VERSION},
            headers={
                "Authorization": f"Bearer {self.credential.get_token(SEARCH_SCOPE).token}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Search {method} {path} failed: {response.text[:1000]}")
        return response.json() if response.content else {}

    def _wait_for_operation(self, response, scope: str) -> None:
        if response.status_code != 202:
            return
        location = response.headers.get("Location") or response.headers.get(
            "Operation-Location"
        )
        if not location:
            raise RuntimeError("Azure operation was accepted without a status URL")
        for _attempt in range(120):
            result = self.http.get(
                location,
                headers={"Authorization": f"Bearer {self.credential.get_token(scope).token}"},
            )
            if result.status_code >= 400:
                raise RuntimeError(f"Azure operation status failed: {result.text[:1000]}")
            operation = result.json()
            status = str(operation.get("status") or "").lower()
            if status in {"succeeded", "completed"}:
                return
            if status in {"failed", "cancelled", "canceled"}:
                raise RuntimeError(f"Azure operation {status}: {result.text[:1000]}")
            import time
            time.sleep(int(result.headers.get("Retry-After", "2")))
        raise RuntimeError("Azure operation did not complete within the reset timeout")


def _drop_order(object_type: str) -> int:
    return {"V": 0, "P": 1, "FN": 2, "IF": 2, "TF": 2, "U": 3}.get(
        object_type, 99
    )


def _path_value(document: dict, path: str):
    value = document
    for component in path.strip("/").split("/"):
        value = value[component]
    return value


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete every legacy Auspex runtime state except owner profile and portfolio ledger"
    )
    parser.add_argument("--cosmos-endpoint", default=os.environ.get("COSMOS_ENDPOINT", ""))
    parser.add_argument("--cosmos-database", default="auspex")
    parser.add_argument("--workspace-id", default=os.environ.get("ONELAKE_WORKSPACE_ID", ""))
    parser.add_argument("--lakehouse-id", default=os.environ.get("ONELAKE_LAKEHOUSE_NAME", ""))
    parser.add_argument("--warehouse-server", default=os.environ.get("FABRIC_WAREHOUSE_SERVER", ""))
    parser.add_argument("--warehouse-database", default=os.environ.get("FABRIC_WAREHOUSE_DATABASE", "auspex_gold"))
    parser.add_argument("--search-endpoint", default=os.environ.get("AI_SEARCH_ENDPOINT", ""))
    parser.add_argument("--export", default="artifacts/legacy-reset/portfolio-preservation.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    require_confirmation(args.apply, args.confirmation)
    required = {
        "cosmos endpoint": args.cosmos_endpoint,
        "workspace id": args.workspace_id,
        "lakehouse id": args.lakehouse_id,
        "warehouse server": args.warehouse_server,
        "search endpoint": args.search_endpoint,
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        parser.error("missing configuration: " + ", ".join(missing))
    reset = LegacyEngineReset(
        cosmos_endpoint=args.cosmos_endpoint,
        cosmos_database=args.cosmos_database,
        workspace_id=args.workspace_id,
        lakehouse_id=args.lakehouse_id,
        warehouse_server=args.warehouse_server,
        warehouse_database=args.warehouse_database,
        search_endpoint=args.search_endpoint,
    )
    plan, preserved = reset.inspect()
    if not args.apply:
        print(json.dumps({
            "status": "dry_run",
            "confirmation_token": CONFIRMATION_TOKEN,
            "plan": asdict(plan),
            "preservation": {
                name: {
                    "count": values["count"],
                    "sha256": values["sha256"],
                }
                for name, values in preservation_manifest(preserved)["containers"].items()
            },
        }, sort_keys=True, default=str))
        return
    result = reset.apply(plan, Path(args.export), preserved)
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()