"""BaseConnector — the contract every source connector must implement."""
from abc import ABC, abstractmethod
from typing import Optional

from .bronze_writer import BronzeWriter
from .control_plane import CosmosControlPlane
from .envelope import deterministic_batch_id, make_envelope
from .models import Batch, RunContext, RunResult, Watermark


class BaseConnector(ABC):
    source_id: str
    schema_version: int

    def __init__(self, cp: CosmosControlPlane, bw: BronzeWriter) -> None:
        self._cp = cp
        self._bw = bw

    @abstractmethod
    def fetch(self, since: Optional[Watermark]) -> Batch: ...

    def run(self, ctx: RunContext) -> RunResult:
        self._cp.start_run(ctx.run_id, ctx.source_id)
        result = self._execute(ctx)
        self._cp.end_run(ctx.run_id, ctx.source_id, result)
        return result

    def _execute(self, ctx: RunContext) -> RunResult:
        wm = self._cp.read_watermark(ctx.source_id)

        try:
            batch = self.fetch(since=wm)
        except Exception as exc:
            return RunResult.failed(str(exc))

        if not batch.records:
            return RunResult.empty()

        batch_id = deterministic_batch_id(self.source_id, batch.window)

        if self._cp.check_dedup(batch_id, ctx.source_id):
            return RunResult.skipped()

        wm_from = wm.last_event_ts if wm else None
        envelopes = [
            make_envelope(r, self.source_id, self.schema_version, batch_id, wm_from)
            for r in batch.records
        ]

        try:
            bytes_written = self._bw.write(self.source_id, batch_id, envelopes)
        except Exception as exc:
            return RunResult.failed(str(exc))

        # Watermark advances only after successful bronze write
        self._cp.advance_watermark(
            ctx.source_id,
            ctx.run_id,
            last_event_ts=batch.new_wm.last_event_ts,
            last_cursor=batch.new_wm.last_cursor,
        )
        self._cp.mark_dedup(batch_id, ctx.source_id)

        return RunResult.ok(records=len(batch.records), bytes_written=bytes_written)
