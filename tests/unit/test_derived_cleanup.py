from collections.abc import AsyncIterator

import pytest

from auspex.cli.derived_cleanup import (
    DERIVED_CLEANUP_TARGETS,
    cleanup_database,
)


class _Container:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.deleted: list[str] = []
        self.queries: list[str] = []

    async def _iterate(self, query: str) -> AsyncIterator[object]:
        if "COUNT(1)" in query:
            yield len(self.rows)
            return
        field = query.split("c.", 1)[1].split(" ", 1)[0]
        for value in dict.fromkeys(row.get(field) for row in self.rows):
            yield value

    def query_items(self, *, query: str) -> AsyncIterator[object]:
        self.queries.append(query)
        return self._iterate(query)

    async def delete_all_items_by_partition_key(
        self,
        *,
        partition_key: str,
    ) -> None:
        self.deleted.append(partition_key)


class _Database:
    def __init__(self) -> None:
        self.containers = {
            target.container: _Container(
                [
                    {
                        "id": f"{target.container}-1",
                        target.partition_key_field: "partition",
                    }
                ]
            )
            for target in DERIVED_CLEANUP_TARGETS
        }

    def get_container_client(self, container: str) -> _Container:
        return self.containers[container]


def test_cleanup_allowlist_excludes_raw_and_user_source_of_truth():
    names = {target.container for target in DERIVED_CLEANUP_TARGETS}

    assert {
        "documents",
        "market_daily",
        "fundamentals",
        "extractions",
        "app_users",
        "user_settings",
        "onboarding",
        "conversations",
        "audit_events",
        "deletion_jobs",
        "watermarks",
        "recommendations",
        "recommendation_dispositions",
        "user_performance",
    }.isdisjoint(names)


@pytest.mark.asyncio
async def test_dry_run_counts_without_deleting():
    database = _Database()

    counts = await cleanup_database(database, apply=False)

    assert counts == {
        target.container: 1 for target in DERIVED_CLEANUP_TARGETS
    }
    assert all(not container.deleted for container in database.containers.values())


@pytest.mark.asyncio
async def test_apply_deletes_each_row_with_its_partition_key():
    database = _Database()

    counts = await cleanup_database(database, apply=True)

    assert sum(counts.values()) == len(DERIVED_CLEANUP_TARGETS)
    for target in DERIVED_CLEANUP_TARGETS:
        assert database.containers[target.container].deleted == ["partition"]


@pytest.mark.asyncio
async def test_missing_partition_key_fails_closed():
    database = _Database()
    database.containers["scores"].rows = [{"id": "score-1"}]

    with pytest.raises(RuntimeError, match="refusing partial cleanup"):
        await cleanup_database(database, apply=True)
    assert all(
        not container.deleted for container in database.containers.values()
    )
