"""Pre-production cleanup of rebuildable engine state.

The allowlist intentionally excludes raw documents, source excerpts, market
observations, fundamentals, users, settings, conversations, audit records, and
the external portfolio ledger. Recommendations, dispositions, and private
performance attribution are also retained because they contain user decisions
or depend on historical recommendations that scoring replay does not recreate.
Everything deleted here is deterministically recreated by bootstrap recovery,
nightly, or performance jobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from auspex.models.market_integrity import MANIFEST_CONFIG_TYPE


@dataclass(frozen=True)
class CleanupTarget:
    container: str
    partition_key_field: str
    where_clause: str = ""

    @property
    def count_query(self) -> str:
        return f"SELECT VALUE COUNT(1) FROM c{self.where_clause}"

    @property
    def partition_query(self) -> str:
        return (
            f"SELECT DISTINCT VALUE c.{self.partition_key_field} "
            f"FROM c{self.where_clause}"
        )


DERIVED_CLEANUP_TARGETS = (
    CleanupTarget("digests", "security_id"),
    CleanupTarget("narratives", "cache_key"),
    CleanupTarget("scores", "security_id"),
    CleanupTarget("leg_changes", "security_id"),
    CleanupTarget("portfolio_projection", "user_id"),
    CleanupTarget("performance", "metric_type"),
    CleanupTarget("runs", "run_date"),
    CleanupTarget(
        "config_versions",
        "config_type",
        f" WHERE c.config_type != '{MANIFEST_CONFIG_TYPE}'",
    ),
)


class Container(Protocol):
    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict] | None = None,
        partition_key: str | None = None,
    ): ...

    async def execute_item_batch(
        self,
        batch_operations: list[tuple[str, tuple[str]]],
        *,
        partition_key: str,
    ): ...


class Database(Protocol):
    def get_container_client(self, container: str) -> Container: ...


async def cleanup_database(
    database: Database,
    *,
    apply: bool,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    planned: list[tuple[Container, str, list[str]]] = []
    for target in DERIVED_CLEANUP_TARGETS:
        container = database.get_container_client(target.container)
        count_rows = [
            row
            async for row in container.query_items(query=target.count_query)
        ]
        if len(count_rows) != 1 or not isinstance(count_rows[0], int):
            raise RuntimeError(
                f"{target.container} count query returned an invalid shape; "
                "refusing cleanup"
            )
        counts[target.container] = count_rows[0]
        partition_keys = [
            value
            async for value in container.query_items(
                query=target.partition_query
            )
        ]
        for partition_key in partition_keys:
            if partition_key is None:
                raise RuntimeError(
                    f"{target.container} row lacks "
                    f"{target.partition_key_field}; refusing partial cleanup"
                )
            resolved_partition = str(partition_key)
            document_ids = [
                value
                async for value in container.query_items(
                    query=(
                        "SELECT VALUE c.id FROM c WHERE "
                        f"c.{target.partition_key_field} = @partition"
                    ),
                    parameters=[
                        {
                            "name": "@partition",
                            "value": partition_key,
                        }
                    ],
                    partition_key=resolved_partition,
                )
            ]
            if any(
                not isinstance(document_id, str) or not document_id
                for document_id in document_ids
            ):
                raise RuntimeError(
                    f"{target.container} partition {resolved_partition} "
                    "contains a row without an id; refusing partial cleanup"
                )
            planned.append(
                (container, resolved_partition, document_ids)
            )
    if apply:
        for container, partition_key, document_ids in planned:
            for offset in range(0, len(document_ids), 100):
                operations = [
                    ("delete", (document_id,))
                    for document_id in document_ids[offset : offset + 100]
                ]
                await container.execute_item_batch(
                    operations,
                    partition_key=partition_key,
                )
    return counts


async def cleanup_derived_command(*, apply: bool = False) -> int:
    from auspex.persistence.cosmos_client import get_cosmos_context

    context = get_cosmos_context()
    try:
        database = await context.database()
        counts = await cleanup_database(database, apply=apply)
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry_run",
                    "targets": counts,
                    "rows": sum(counts.values()),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await context.aclose()
