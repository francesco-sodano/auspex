import gzip
import json
from datetime import date

import pytest

from auspex.cli.engine_baseline import (
    build_baseline_archive,
    export_engine_baseline_command,
    shared_performance_rows,
)


def test_baseline_archive_is_deterministic_and_sorted():
    scores = [
        {"id": "b", "security_id": "b", "as_of_date": "2026-01-02"},
        {"id": "a", "security_id": "a", "as_of_date": "2026-01-01"},
    ]
    performance = [
        {
            "id": "metric",
            "metric_type": "composite_ic",
            "scope": "universe",
            "as_of_date": "2026-01-02",
        }
    ]

    first, first_manifest = build_baseline_archive(
        label="v4.1.0",
        scores=scores,
        performance=performance,
        exported_on=date(2026, 8, 20),
    )
    second, second_manifest = build_baseline_archive(
        label="v4.1.0",
        scores=list(reversed(scores)),
        performance=performance,
        exported_on=date(2026, 8, 20),
    )

    assert first == second
    assert first_manifest == second_manifest
    rows = [
        json.loads(line)
        for line in gzip.decompress(first).decode("utf-8").splitlines()
    ]
    assert rows[0]["score_rows"] == 2
    assert [row["document"]["id"] for row in rows[1:3]] == ["a", "b"]


@pytest.mark.parametrize("label", ["", "../escape", "space label", "x" * 65])
def test_baseline_label_rejects_unsafe_values(label):
    with pytest.raises(ValueError):
        build_baseline_archive(
            label=label,
            scores=[],
            performance=[],
            exported_on=date(2026, 8, 20),
        )


def test_private_performance_metrics_are_excluded():
    rows = [
        {"metric_type": "composite_ic", "user_id": None},
        {
            "metric_type": "suggestion_hit_rate",
            "user_id": "user-a",
        },
        {
            "metric_type": "disposition_outcome",
            "user_id": "user-b",
        },
        {"metric_type": "leg_ic"},
    ]

    assert shared_performance_rows(rows) == [rows[0], rows[3]]


def test_unknown_metric_types_fail_closed():
    rows = [
        {"metric_type": "future_user_metric", "user_id": None},
        {"metric_type": "composite_ic", "user_id": None},
    ]

    assert shared_performance_rows(rows) == [rows[1]]


def test_user_attributed_score_rows_are_rejected():
    with pytest.raises(ValueError, match="user-attributed"):
        build_baseline_archive(
            label="v4.1.0",
            scores=[
                {
                    "id": "private",
                    "security_id": "x",
                    "user_id": "user-a",
                }
            ],
            performance=[],
            exported_on=date(2026, 8, 20),
        )


class _Container:
    def __init__(self, rows):
        self.rows = rows

    def query_items(self, query):
        async def results():
            for row in self.rows:
                yield row

        return results()


class _Database:
    def __init__(self, scores, performance):
        self.containers = {
            "scores": _Container(scores),
            "performance": _Container(performance),
        }

    def get_container_client(self, name):
        return self.containers[name]


class _Cosmos:
    def __init__(self, scores, performance):
        self.database_value = _Database(scores, performance)
        self.closed = False

    async def database(self):
        return self.database_value

    async def aclose(self):
        self.closed = True


class _Blob:
    def __init__(self):
        self.closed = False

    async def upload_export_stream(self, *args):
        return "exports/system/archive"

    async def upload_export_blob(self, *args):
        return "exports/system/manifest"

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_production_export_rejects_private_score_row(monkeypatch):
    cosmos = _Cosmos(
        [{"id": "score", "user_id": "user-a"}],
        [],
    )
    blob = _Blob()
    monkeypatch.setattr(
        "auspex.persistence.cosmos_client.get_cosmos_context",
        lambda: cosmos,
    )
    monkeypatch.setattr(
        "auspex.persistence.blob_client.get_blob_context",
        lambda: blob,
    )

    with pytest.raises(RuntimeError, match="user-attributed score"):
        await export_engine_baseline_command("v4.1.0")

    assert cosmos.closed and blob.closed


@pytest.mark.asyncio
async def test_production_export_rejects_private_performance_row(monkeypatch):
    cosmos = _Cosmos(
        [{"id": "score", "security_id": "security"}],
        [
            {
                "id": "private",
                "metric_type": "future_user_metric",
                "user_id": "user-a",
            }
        ],
    )
    blob = _Blob()
    monkeypatch.setattr(
        "auspex.persistence.cosmos_client.get_cosmos_context",
        lambda: cosmos,
    )
    monkeypatch.setattr(
        "auspex.persistence.blob_client.get_blob_context",
        lambda: blob,
    )

    with pytest.raises(RuntimeError, match="non-shared performance"):
        await export_engine_baseline_command("v4.1.0")

    assert cosmos.closed and blob.closed
