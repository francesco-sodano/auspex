"""BaseConnector — the contract every source connector must implement."""
from abc import ABC, abstractmethod
import re
from typing import Optional

from .bronze_writer import BronzeWriter
from .control_plane import CosmosControlPlane
from .envelope import deterministic_batch_id, make_envelope
from .models import Batch, RunContext, RunResult, Watermark


_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|code|sig|secret)=)[^&\s'\"]+|(?:Bearer\s+)[A-Za-z0-9._~+\-/]+=*"
)


def _redact_error(error: object) -> str:
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.group(1) else "Bearer [REDACTED]", str(error))


class BaseConnector(ABC):
    source_id: str
    schema_version: int

    def __init__(self, cp: CosmosControlPlane, bw: BronzeWriter, source_config: Optional[dict] = None) -> None:
        self._cp = cp
        self._bw = bw
        self._source_config = source_config or {}

    @abstractmethod
    def fetch(self, since: Optional[Watermark]) -> Batch: ...

    @property
    def watermark_source_id(self) -> str:
        return self.source_id

    def run(self, ctx: RunContext) -> RunResult:
        started = False
        result = RunResult.failed("run did not complete")
        try:
            self._cp.start_run(ctx.run_id, ctx.source_id)
            started = True
            result = self._execute(ctx)
        except Exception as exc:
            result = RunResult.failed(_redact_error(exc))
        finally:
            if started:
                try:
                    self._cp.end_run(ctx.run_id, ctx.source_id, result)
                except Exception as exc:
                    result = RunResult.failed(f"run log update failed: {_redact_error(exc)}")
        return result

    def _execute(self, ctx: RunContext) -> RunResult:
        wm = self._cp.read_watermark(self.watermark_source_id)

        try:
            batch = self.fetch(since=wm)
        except Exception as exc:
            return RunResult.failed(_redact_error(exc))

        if not batch.records:
            return RunResult.empty(
                has_more=batch.has_more,
                last_event_ts=batch.new_wm.last_event_ts,
                last_cursor=batch.new_wm.last_cursor,
            )

        batch_id = deterministic_batch_id(
            self.source_id,
            batch.window,
            self.schema_version,
        )
        partition_date = self._partition_date(batch)

        if self._cp.check_dedup(batch_id, ctx.source_id):
            try:
                self._advance_watermark(ctx, batch)
            except Exception as exc:
                return RunResult.failed(_redact_error(exc))
            return RunResult.skipped(
                has_more=batch.has_more,
                last_event_ts=batch.new_wm.last_event_ts,
                last_cursor=batch.new_wm.last_cursor,
            )

        wm_from = batch.watermark_from if ctx.mode == "backfill" else (wm.last_event_ts if wm else None)
        envelopes = [
            make_envelope(r, self.source_id, self.schema_version, batch_id, wm_from)
            for r in batch.records
        ]

        try:
            bytes_written = self._bw.write(self.source_id, batch_id, envelopes, partition_date)
        except Exception as exc:
            return RunResult.failed(_redact_error(exc))

        try:
            self.after_bronze_write(batch)
        except Exception as exc:
            return RunResult.failed(_redact_error(exc))

        # Mark bronze landing before the watermark. If the watermark update fails,
        # a replay sees the dedup marker and advances the watermark without rewriting.
        try:
            self._cp.mark_dedup(batch_id, ctx.source_id)
            self._advance_watermark(ctx, batch)
        except Exception as exc:
            return RunResult.failed(_redact_error(exc))

        return RunResult.ok(
            records=len(batch.records),
            bytes_written=bytes_written,
            has_more=batch.has_more,
            last_event_ts=batch.new_wm.last_event_ts,
            last_cursor=batch.new_wm.last_cursor,
        )

    def after_bronze_write(self, batch: Batch) -> None:
        return None

    def _advance_watermark(self, ctx: RunContext, batch: Batch) -> None:
        if ctx.mode == "backfill" or batch.has_more:
            return
        self._cp.advance_watermark(
            self.watermark_source_id,
            ctx.run_id,
            last_event_ts=batch.new_wm.last_event_ts,
            last_cursor=batch.new_wm.last_cursor,
        )

    def _partition_date(self, batch: Batch) -> str:
        if batch.partition_date:
            return batch.partition_date
        if batch.new_wm.last_event_ts:
            return batch.new_wm.last_event_ts[:10]
        raise ValueError("Batch partition_date or new_wm.last_event_ts is required for bronze writes")

    def _requests_per_minute(self, default: int) -> int:
        rate_limit = self._source_config.get("rate_limit") or {}
        return int(rate_limit.get("requests_per_minute") or default)
