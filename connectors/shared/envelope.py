from datetime import datetime, timezone
from typing import Optional


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_batch_id(source_id: str, window: str) -> str:
    return f"{source_id}-{window}"


def make_envelope(
    record: dict,
    source_id: str,
    schema_version: int,
    batch_id: str,
    watermark_from: Optional[str],
) -> dict:
    return {
        "ingest_ts": _now_utc(),
        "source_id": source_id,
        "schema_version": schema_version,
        "batch_id": batch_id,
        "watermark_from": watermark_from,
        "record": record,
    }
