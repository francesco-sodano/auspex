"""Multi-user nightly orchestration (arc42 §6.1).

The nightly run is one shared research pass, a bounded per-user fan-out, and
a shared finalisation pass:

*Shared, once per night.* Ingestion, AI extraction, leg computation, cohort
assignment, normalisation, diffing and snapshot writing are identical for
everyone. They are also the expensive parts (provider quota, LLM tokens), so
running them per user would multiply cost for no information gain. Global
score performance therefore stays a shared, population-level measurement.

*Per user.* The policy gate cascade, its post-run assertions and the
portfolio projection depend entirely on one user's own ledger and settings.
These run once per ``ACTIVE`` user against a context derived from the shared
one, with a ledger binding that can only see that user's partition.

*Shared finalisation.* Narrative generation, the final validation gate and
the run manifest close the night. These deliberately run **after** the
fan-out: ``VALIDATE`` reconciles recommendation counts and ``END_RUN``
summarises the resulting action mix, so running them before any policy had
executed would report a spurious degradation every single night.

The three phases are contiguous slices of ``PIPELINE_STEPS``, so the step
order a run records is exactly the documented order.

*Failure isolation.* One user's stage failing — an unreadable ledger,
malformed settings, a transient Cosmos error — must never deny every other
user their nightly recommendations. Each user's stage is therefore wrapped
individually; failures are collected, reported on the run manifest as a
degradation, and the run continues.

*Bounded concurrency.* ``Settings.nightly_user_concurrency`` caps how many
users are processed at once so a large roster cannot exhaust request units
or provider connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from auspex.models.common import utc_now
from auspex.models.enums import RunStatus
from auspex.models.run import PIPELINE_STEPS, RunManifest
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.manifest import complete_step, fail_step, new_manifest, resume_step_index
from auspex.pipeline.runner import STEP_FUNCTIONS
from auspex.portfolio.port import PortfolioPort

logger = logging.getLogger("auspex.pipeline.fanout")


@asynccontextmanager
async def _unfenced_user_operation() -> AsyncIterator[None]:
    yield

#: Steps whose output depends on a single user's portfolio and settings.
#: Contiguous in ``PIPELINE_STEPS`` by construction.
PER_USER_STEPS: tuple[str, ...] = ("RUN_POLICY", "ASSERT", "PROJECT_PORTFOLIO")

_FIRST_PER_USER = PIPELINE_STEPS.index(PER_USER_STEPS[0])
_LAST_PER_USER = PIPELINE_STEPS.index(PER_USER_STEPS[-1])

#: Universe-wide research, computed once before any user is considered.
SHARED_PRE_STEPS: tuple[str, ...] = tuple(PIPELINE_STEPS[:_FIRST_PER_USER])

#: Narration, validation and run closure — shared, but only meaningful once
#: every user's policy stage has produced its recommendations.
SHARED_POST_STEPS: tuple[str, ...] = tuple(PIPELINE_STEPS[_LAST_PER_USER + 1 :])

#: Everything that is not per-user.
SHARED_STEPS: tuple[str, ...] = SHARED_PRE_STEPS + SHARED_POST_STEPS


@dataclass
class UserStageResult:
    user_id: str
    succeeded: bool
    detail: str | None = None
    scratch: dict = field(default_factory=dict)


@dataclass
class MultiUserRunResult:
    manifest: RunManifest
    user_results: list[UserStageResult] = field(default_factory=list)

    @property
    def failed_user_ids(self) -> list[str]:
        return [result.user_id for result in self.user_results if not result.succeeded]

    @property
    def succeeded_user_ids(self) -> list[str]:
        return [result.user_id for result in self.user_results if result.succeeded]


async def run_shared_stage(
    ctx: PipelineContext,
    manifest: RunManifest,
    step_names: tuple[str, ...] = SHARED_STEPS,
) -> RunManifest:
    """Run the given shared steps in order, checkpointing as it goes.

    Resume semantics are unchanged: a step already recorded SUCCESS or
    SKIPPED is not re-run, so an interrupted night picks up where it stopped.
    """

    start_index = resume_step_index(manifest)
    deadline_seconds = ctx.hard_timeout_minutes * 60

    for index, step_name in enumerate(PIPELINE_STEPS):
        if step_name not in step_names or index < start_index:
            continue
        elapsed = (utc_now() - manifest.started_at).total_seconds()
        if elapsed > deadline_seconds:
            manifest.status = RunStatus.TIMEOUT
            manifest.finished_at = utc_now()
            manifest.watermarks_committed = False
            return manifest

        try:
            await STEP_FUNCTIONS[step_name](ctx, manifest)
        except Exception as exc:  # noqa: BLE001 - checkpoint the failure, do not corrupt state
            fail_step(manifest, step_name, detail=str(exc))
            manifest.status = RunStatus.FAILED
            manifest.finished_at = utc_now()
            if ctx.repos.run_repo is not None:
                await ctx.repos.run_repo.upsert(manifest)
            return manifest
        if ctx.repos.run_repo is not None:
            await ctx.repos.run_repo.upsert(manifest)
    return manifest


async def run_user_stage(
    shared_ctx: PipelineContext,
    manifest: RunManifest,
    user_id: str,
    *,
    portfolio_reader: PortfolioPort | None = None,
) -> UserStageResult:
    """Run the per-user steps for one user against their own ledger.

    The shared manifest is a coarse record of the whole night; per-user step
    checkpoints would overwrite each other, so this reports through
    :class:`UserStageResult` and lets the caller summarise once at the end.
    """

    ctx = shared_ctx.derive_for_user(user_id, portfolio_reader=portfolio_reader)
    scratch_manifest = new_manifest(shared_ctx.as_of_date, run_type=f"nightly-user-{user_id}")
    try:
        for step_name in PER_USER_STEPS:
            await STEP_FUNCTIONS[step_name](ctx, scratch_manifest)
    except Exception as exc:  # noqa: BLE001 - one user must never fail the whole night
        logger.error("nightly: per-user stage failed for %s", user_id, exc_info=True)
        return UserStageResult(user_id=user_id, succeeded=False, detail=str(exc))
    scratch = {
        key: ctx.__dict__[key]
        for key in PipelineContext.PER_USER_SCRATCH_KEYS
        if key in ctx.__dict__
    }
    violations = scratch.get("_assertion_violations") or []
    detail = "; ".join(f"{v.name}: {v.detail}" for v in violations) or None
    return UserStageResult(user_id=user_id, succeeded=True, detail=detail, scratch=scratch)


async def run_multi_user_pipeline(
    shared_ctx: PipelineContext,
    user_ids: list[str],
    *,
    portfolio_reader_factory: Callable[[str], PortfolioPort | None] | None = None,
    user_operation_factory: (
        Callable[[str], AbstractAsyncContextManager] | None
    ) = None,
    concurrency: int = 4,
    existing_manifest: RunManifest | None = None,
) -> MultiUserRunResult:
    """Shared research, a bounded per-user fan-out, then shared closure."""

    manifest = existing_manifest or new_manifest(shared_ctx.as_of_date)
    manifest = await run_shared_stage(shared_ctx, manifest, SHARED_PRE_STEPS)
    if manifest.status in (RunStatus.FAILED, RunStatus.TIMEOUT):
        return MultiUserRunResult(manifest=manifest)

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(user_id: str) -> UserStageResult:
        async with semaphore:
            try:
                operation = (
                    user_operation_factory(user_id)
                    if user_operation_factory
                    else _unfenced_user_operation()
                )
                async with operation:
                    reader = (
                        portfolio_reader_factory(user_id)
                        if portfolio_reader_factory
                        else None
                    )
                    return await run_user_stage(
                        shared_ctx,
                        manifest,
                        user_id,
                        portfolio_reader=reader,
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one user
                logger.error(
                    "nightly: user operation fence failed for %s",
                    user_id,
                    exc_info=True,
                )
                return UserStageResult(
                    user_id=user_id,
                    succeeded=False,
                    detail=str(exc),
                )

    results = list(await asyncio.gather(*(run_one(user_id) for user_id in user_ids)))
    _record_user_stage(manifest, results)
    _adopt_representative_scratch(shared_ctx, user_ids, results)

    manifest = await run_shared_stage(shared_ctx, manifest, SHARED_POST_STEPS)

    if shared_ctx.repos.run_repo is not None:
        await shared_ctx.repos.run_repo.upsert(manifest)
    return MultiUserRunResult(manifest=manifest, user_results=results)


def _adopt_representative_scratch(
    shared_ctx: PipelineContext, user_ids: list[str], results: list[UserStageResult]
) -> None:
    """Expose one user's policy outcome to the shared closing steps.

    ``VALIDATE`` reconciles recommendation counts and ``END_RUN`` summarises
    the action mix; both need *a* concrete user's outcome to describe. The
    context's own user is preferred, falling back to the first user whose
    stage succeeded — which makes a single-user run behave exactly as it did
    before the fan-out existed.
    """

    by_user = {result.user_id: result for result in results if result.succeeded}
    preferred = shared_ctx.user_id if shared_ctx.user_id in by_user else None
    if preferred is None:
        preferred = next((user_id for user_id in user_ids if user_id in by_user), None)
    if preferred is None:
        return
    shared_ctx.user_id = preferred
    shared_ctx.__dict__.update(by_user[preferred].scratch)


def _record_user_stage(manifest: RunManifest, results: list[UserStageResult]) -> None:
    """Summarise the fan-out onto the shared manifest.

    Each per-user step is marked complete with a count of how many users it
    ran for; any user failure degrades the run rather than failing it, since
    the shared research and every other user's recommendations are perfectly
    usable.
    """

    failed = [result for result in results if not result.succeeded]
    succeeded = len(results) - len(failed)
    detail = f"users={len(results)} succeeded={succeeded} failed={len(failed)}"
    for step_name in PER_USER_STEPS:
        complete_step(manifest, step_name, detail=detail, degraded=bool(failed))
