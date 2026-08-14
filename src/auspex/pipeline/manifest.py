"""Run manifest and step checkpointing (arc42 §6.1).

Every write upserts on ``security_id + as_of_date`` (idempotency); a failed
run resumes from the last successful step. Watermarks advance only at step
20 (``END_RUN``).
"""

from __future__ import annotations

from datetime import date

from auspex.models.common import utc_now
from auspex.models.enums import RunStatus
from auspex.models.run import PIPELINE_STEPS, RunManifest, StepCheckpoint


def new_manifest(run_date: date, run_type: str = "nightly", lease_owner: str | None = None) -> RunManifest:
    return RunManifest(
        id=f"{run_date.isoformat()}:{run_type}",
        run_date=run_date,
        run_type=run_type,
        status=RunStatus.RUNNING,
        started_at=utc_now(),
        steps=[StepCheckpoint(step=name, status="PENDING") for name in PIPELINE_STEPS],
        lease_owner=lease_owner,
    )


def start_step(manifest: RunManifest, step: str) -> None:
    cp = manifest.step_by_name(step)
    if cp is None:  # pragma: no cover - defensive
        raise ValueError(f"unknown pipeline step: {step}")
    cp.status = "RUNNING"
    cp.started_at = utc_now()


def complete_step(manifest: RunManifest, step: str, *, detail: str | None = None, degraded: bool = False) -> None:
    cp = manifest.step_by_name(step)
    if cp is None:  # pragma: no cover - defensive
        raise ValueError(f"unknown pipeline step: {step}")
    cp.status = "SUCCESS"
    cp.finished_at = utc_now()
    cp.detail = detail
    cp.degraded = degraded
    if degraded and step not in manifest.degraded_reasons:
        manifest.degraded_reasons.append(f"{step}: {detail or 'degraded'}")


def fail_step(manifest: RunManifest, step: str, *, detail: str) -> None:
    cp = manifest.step_by_name(step)
    if cp is None:  # pragma: no cover - defensive
        raise ValueError(f"unknown pipeline step: {step}")
    cp.status = "FAILED"
    cp.finished_at = utc_now()
    cp.detail = detail


def skip_step(manifest: RunManifest, step: str, *, detail: str) -> None:
    cp = manifest.step_by_name(step)
    if cp is None:  # pragma: no cover - defensive
        raise ValueError(f"unknown pipeline step: {step}")
    cp.status = "SKIPPED"
    cp.finished_at = utc_now()
    cp.detail = detail


def resume_step_index(manifest: RunManifest) -> int:
    """Index of the first step that still needs to run (idempotent resume).

    A step counts as already done if its status is SUCCESS or SKIPPED; a
    RUNNING step from a crashed process is treated as needing a re-run.
    """

    for i, cp in enumerate(manifest.steps):
        if cp.status not in ("SUCCESS", "SKIPPED"):
            return i
    return len(manifest.steps)


def finalize(manifest: RunManifest, *, status: RunStatus, watermarks_committed: bool) -> None:
    manifest.status = status
    manifest.finished_at = utc_now()
    manifest.watermarks_committed = watermarks_committed
