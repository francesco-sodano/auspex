"""Nightly pipeline runner (arc42 §6.1).

Runs the 20 steps in order, checkpointing to the run manifest after each
step. A failed run resumes from the last successful step on the next
invocation for the same ``as_of_date``. Hard timeout terminates the run with
``status=TIMEOUT`` and does not commit watermarks.
"""

from __future__ import annotations

from auspex.models.common import utc_now
from auspex.models.enums import RunStatus
from auspex.models.run import PIPELINE_STEPS, RunManifest
from auspex.pipeline import steps as step_fns
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.manifest import fail_step, new_manifest, resume_step_index

STEP_FUNCTIONS = {
    "START_RUN": step_fns.step_start_run,
    "COLLECT_PRICES": step_fns.step_collect_prices,
    "COLLECT_FX": step_fns.step_collect_fx,
    "COLLECT_FILINGS": step_fns.step_collect_filings,
    "COLLECT_INSIDERS": step_fns.step_collect_insiders,
    "COLLECT_NEWS": step_fns.step_collect_news,
    "COLLECT_FUNDAMENTALS": step_fns.step_collect_fundamentals,
    "EXTRACT_CHANNEL_A": step_fns.step_extract_channel_a,
    "EXTRACT_CHANNEL_B": step_fns.step_extract_channel_b,
    "COMPUTE_RAW_LEGS": step_fns.step_compute_raw_legs,
    "ASSIGN_COHORTS": step_fns.step_assign_cohorts,
    "NORMALISE": step_fns.step_normalise,
    "DIFF": step_fns.step_diff,
    "WRITE_SNAPSHOT": step_fns.step_write_snapshot,
    "RUN_POLICY": step_fns.step_run_policy,
    "ASSERT": step_fns.step_assert,
    "PROJECT_PORTFOLIO": step_fns.step_project_portfolio,
    "NARRATE": step_fns.step_narrate,
    "VALIDATE": step_fns.step_validate,
    "END_RUN": step_fns.step_end_run,
}


class PipelineRunner:
    def __init__(self, context: PipelineContext) -> None:
        self._ctx = context

    async def run(self, existing_manifest: RunManifest | None = None) -> RunManifest:
        manifest = existing_manifest or new_manifest(self._ctx.as_of_date)
        start_index = resume_step_index(manifest)
        deadline_seconds = self._ctx.hard_timeout_minutes * 60

        for i in range(start_index, len(PIPELINE_STEPS)):
            step_name = PIPELINE_STEPS[i]
            elapsed = (utc_now() - manifest.started_at).total_seconds()
            if elapsed > deadline_seconds:
                manifest.status = RunStatus.TIMEOUT
                manifest.finished_at = utc_now()
                manifest.watermarks_committed = False
                return manifest

            fn = STEP_FUNCTIONS[step_name]
            try:
                await fn(self._ctx, manifest)
                if self._ctx.repos.run_repo is not None:
                    await self._ctx.repos.run_repo.upsert(manifest)
            except Exception as exc:  # noqa: BLE001 - checkpoint the failure, do not corrupt state
                fail_step(manifest, step_name, detail=str(exc))
                manifest.status = RunStatus.FAILED
                manifest.finished_at = utc_now()
                if self._ctx.repos.run_repo is not None:
                    await self._ctx.repos.run_repo.upsert(manifest)
                return manifest

        return manifest


async def run_nightly_pipeline(context: PipelineContext, existing_manifest: RunManifest | None = None) -> RunManifest:
    runner = PipelineRunner(context)
    return await runner.run(existing_manifest)
