from datetime import datetime, timezone
import re
from typing import Optional


_UNSAFE_BATCH_ID_CHARS = re.compile(r"[^A-Za-z0-9._=-]+")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_batch_id(
    source_id: str,
    window: str,
    schema_version: int | None = None,
) -> str:
    safe_window = _UNSAFE_BATCH_ID_CHARS.sub("-", window).strip("-")
    version = f"-v{schema_version}" if schema_version is not None else ""
    return f"{source_id}{version}-{safe_window}"


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
