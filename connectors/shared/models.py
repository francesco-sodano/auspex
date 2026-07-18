from dataclasses import dataclass
from typing import Optional


@dataclass
class Watermark:
    source_id: str
    last_event_ts: Optional[str] = None
    last_cursor: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Batch:
    records: list
    new_wm: Watermark
    window: str  # deterministic string used in batch_id
    partition_date: Optional[str] = None  # YYYY-MM-DD used for deterministic bronze path


@dataclass
class RunContext:
    run_id: str
    source_id: str
    mode: str = "run"


@dataclass
class RunResult:
    status: str  # ok | partial | failed | skipped | empty
    records_in: int = 0
    bytes_written: int = 0
    error: Optional[str] = None

    @classmethod
    def ok(cls, records: int, bytes_written: int = 0) -> "RunResult":
        return cls(status="ok", records_in=records, bytes_written=bytes_written)

    @classmethod
    def empty(cls) -> "RunResult":
        return cls(status="empty")

    @classmethod
    def skipped(cls) -> "RunResult":
        return cls(status="skipped")

    @classmethod
    def failed(cls, error: str) -> "RunResult":
        return cls(status="failed", error=error)
