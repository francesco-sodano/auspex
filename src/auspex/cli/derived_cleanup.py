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
    query: str = "SELECT * FROM c"


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
        (
            "SELECT * FROM c WHERE c.config_type != "
            f"'{MANIFEST_CONFIG_TYPE}'"
        ),
    ),
)


class Container(Protocol):
    def query_items(self, *, query: str): ...

    async def delete_item(self, *, item: str, partition_key: str) -> None: ...


class Database(Protocol):
    def get_container_client(self, container: str) -> Container: ...


async def cleanup_database(
    database: Database,
    *,
    apply: bool,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    planned: list[tuple[Container, str, str]] = []
    for target in DERIVED_CLEANUP_TARGETS:
        container = database.get_container_client(target.container)
        rows = [
            row
            async for row in container.query_items(query=target.query)
        ]
        counts[target.container] = len(rows)
        for row in rows:
            partition_key = row.get(target.partition_key_field)
            document_id = row.get("id")
            if not document_id or partition_key is None:
                raise RuntimeError(
                    f"{target.container} row lacks id or "
                    f"{target.partition_key_field}; refusing partial cleanup"
                )
            planned.append(
                (container, str(document_id), str(partition_key))
            )
    if apply:
        for container, document_id, partition_key in planned:
            await container.delete_item(
                item=document_id,
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
