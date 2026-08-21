"""Immutable export of the score/performance champion before a replay."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import tempfile
from datetime import date

from auspex.models.common import utc_now

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHARED_METRIC_TYPES = frozenset(
    {
        "composite_ic",
        "leg_ic",
        "leg_correlation",
        "cohort_quality",
        "ic_distribution",
        "ic_interval",
        "spread",
        "benchmark",
        "coverage_bias",
        "multiple_testing",
        "shadow_comparison",
    }
)


def validate_label(label: str) -> None:
    if not _LABEL.fullmatch(label):
        raise ValueError(
            "baseline label must be 1-64 safe filename characters"
        )


def shared_performance_rows(rows: list[dict]) -> list[dict]:
    """Exclude every user-attributed/private metric from a shared baseline."""

    return [
        row
        for row in rows
        if not row.get("user_id")
        and row.get("metric_type") in SHARED_METRIC_TYPES
    ]


def shared_performance_query(select: str = "*") -> str:
    metric_types = ",".join(
        f"'{metric_type}'"
        for metric_type in sorted(SHARED_METRIC_TYPES)
    )
    return (
        f"SELECT {select} FROM c WHERE "
        "(NOT IS_DEFINED(c.user_id) OR IS_NULL(c.user_id)) "
        f"AND c.metric_type IN ({metric_types})"
    )


def build_baseline_archive(
    *,
    label: str,
    scores: list[dict],
    performance: list[dict],
    exported_on: date,
) -> tuple[bytes, dict[str, object]]:
    """Return deterministic gzip JSONL plus a small verification manifest."""

    validate_label(label)
    safe_scores = [
        row
        for row in scores
        if row.get("user_id") is None
    ]
    if len(safe_scores) != len(scores):
        raise ValueError("shared score baseline contains user-attributed rows")
    ordered_scores = sorted(
        safe_scores,
        key=lambda row: (
            str(row.get("as_of_date", "")),
            str(row.get("security_id", "")),
            str(row.get("id", "")),
        ),
    )
    ordered_performance = sorted(
        shared_performance_rows(performance),
        key=lambda row: (
            str(row.get("as_of_date", "")),
            str(row.get("metric_type", "")),
            str(row.get("scope", "")),
            str(row.get("id", "")),
        ),
    )
    header = {
        "kind": "auspex_engine_baseline",
        "label": label,
        "exported_on": exported_on.isoformat(),
        "score_rows": len(ordered_scores),
        "performance_rows": len(ordered_performance),
    }
    lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
    lines.extend(
        json.dumps(
            {"kind": "score", "document": row},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in ordered_scores
    )
    lines.extend(
        json.dumps(
            {"kind": "performance", "document": row},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in ordered_performance
    )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    compressed = gzip.compress(payload, mtime=0)
    manifest = {
        **header,
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "compressed_bytes": len(compressed),
    }
    return compressed, manifest


async def export_engine_baseline_command(label: str) -> int:
    """Export shared score/performance rows to the protected exports container."""

    validate_label(label)
    from auspex.persistence.blob_client import get_blob_context
    from auspex.persistence.cosmos_client import get_cosmos_context

    cosmos = get_cosmos_context()
    blob = get_blob_context()
    try:
        database = await cosmos.database()

        exported_on = utc_now().date()
        score_container = database.get_container_client("scores")
        performance_container = database.get_container_client("performance")
        score_count = 0
        performance_count = 0
        stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        with gzip.GzipFile(
            fileobj=stream,
            mode="wb",
            mtime=0,
        ) as archive:
            async for row in score_container.query_items(
                query=(
                    "SELECT * FROM c WHERE "
                    "NOT IS_DEFINED(c.user_id) OR IS_NULL(c.user_id)"
                ),
            ):
                if row.get("user_id") is not None:
                    raise RuntimeError(
                        "refusing to export user-attributed score row"
                    )
                score_count += 1
                archive.write(
                    (
                        json.dumps(
                            {"kind": "score", "document": row},
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            async for row in performance_container.query_items(
                query=shared_performance_query(),
            ):
                if (
                    row.get("user_id")
                    or row.get("metric_type") not in SHARED_METRIC_TYPES
                ):
                    raise RuntimeError(
                        "refusing to export non-shared performance row"
                    )
                performance_count += 1
                archive.write(
                    (
                        json.dumps(
                            {
                                "kind": "performance",
                                "document": row,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            header = {
                "kind": "auspex_engine_baseline_manifest",
                "label": label,
                "exported_on": exported_on.isoformat(),
                "score_rows": score_count,
                "performance_rows": performance_count,
            }
            archive.write(
                (
                    json.dumps(
                        header,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        compressed_bytes = stream.tell()
        manifest = {
            **header,
            "sha256": digest.hexdigest(),
            "compressed_bytes": compressed_bytes,
        }
        stream.seek(0)
        blob_name = f"engine-baseline-{label}-{exported_on.isoformat()}"
        archive_path = await blob.upload_export_stream(
            "system",
            blob_name,
            "jsonl.gz",
            stream,
        )
        stream.close()
        manifest_path = await blob.upload_export_blob(
            "system",
            blob_name,
            "manifest.json",
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
        print(
            json.dumps(
                {
                    **manifest,
                    "archive_path": archive_path,
                    "manifest_path": manifest_path,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await blob.aclose()
        await cosmos.aclose()
