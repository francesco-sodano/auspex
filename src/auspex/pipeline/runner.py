"""Nightly pipeline runner (arc42 §6.1).

Runs the 20 steps in order, checkpointing to the run manifest after each
step. A failed run resumes from the last successful step on the next
invocation for the same ``as_of_date``. Hard timeout terminates the run with
``status=TIMEOUT`` and does not commit watermarks — and is enforced *within*
each step, not merely between them, so a step that hangs on a provider call
cannot outlive the configured budget.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

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
    "PROJECT_PORTFOLIO": step_fns.step_project_portfolio,
    "RUN_POLICY": step_fns.step_run_policy,
    "ASSERT": step_fns.step_assert,
    "NARRATE": step_fns.step_narrate,
    "VALIDATE": step_fns.step_validate,
    "END_RUN": step_fns.step_end_run,
}


async def run_step_bounded(
    ctx: PipelineContext,
    manifest: RunManifest,
    step_name: str,
    *,
    deadline_from: datetime | None = None,
) -> None:
    """Run one step under the context's per-step budget.

    ``deadline_from`` anchors the *whole-run* half of that budget. It exists for
    the per-user fan-out, which checkpoints onto a throwaway scratch manifest
    created fresh for each user: measuring elapsed time against that manifest
    would restart the run clock per user, so the last user of a long night would
    be handed a full budget the night no longer has. Callers pass the shared
    run's ``started_at`` instead; everything else defaults to the manifest it is
    checkpointing onto.

    Raises :class:`TimeoutError` when the step exceeds its budget; callers
    translate that into ``status=TIMEOUT`` with watermarks uncommitted.
    """

    anchor = deadline_from if deadline_from is not None else manifest.started_at
    budget = ctx.step_budget_seconds((utc_now() - anchor).total_seconds())
    await asyncio.wait_for(STEP_FUNCTIONS[step_name](ctx, manifest), timeout=budget)


def _mark_timed_out(manifest: RunManifest) -> RunManifest:
    manifest.status = RunStatus.TIMEOUT
    manifest.finished_at = utc_now()
    manifest.watermarks_committed = False
    return manifest


class PipelineRunner:
    def __init__(self, context: PipelineContext) -> None:
        self._ctx = context

    async def run(self, existing_manifest: RunManifest | None = None) -> RunManifest:
        manifest = existing_manifest or new_manifest(self._ctx.as_of_date)
        start_index = resume_step_index(manifest)
        deadline_seconds = self._ctx.hard_timeout_minutes * 60
        if (
            PIPELINE_STEPS.index("RUN_POLICY") >= start_index
            and self._ctx.config.get("policy", {})
            .get("allocation", {})
            .get("shadow_risk_aware", False)
        ):
            from auspex.pipeline.fanout import prepare_market_risk_context

            await prepare_market_risk_context(self._ctx)

        for i in range(start_index, len(PIPELINE_STEPS)):
            step_name = PIPELINE_STEPS[i]
            elapsed = (utc_now() - manifest.started_at).total_seconds()
            if elapsed > deadline_seconds:
                return _mark_timed_out(manifest)

            try:
                await run_step_bounded(self._ctx, manifest, step_name)
                if self._ctx.repos.run_repo is not None:
                    await self._ctx.repos.run_repo.upsert(manifest)
            except TimeoutError:
                fail_step(manifest, step_name, detail="step exceeded the pipeline timeout budget")
                _mark_timed_out(manifest)
                if self._ctx.repos.run_repo is not None:
                    await self._ctx.repos.run_repo.upsert(manifest)
                return manifest
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
