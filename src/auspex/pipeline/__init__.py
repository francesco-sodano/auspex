"""20-step idempotent nightly pipeline with manifests/checkpointing (arc42 §6.1)."""

from __future__ import annotations

from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.fanout import (
    PER_USER_STEPS,
    SHARED_STEPS,
    MultiUserRunResult,
    UserStageResult,
    run_multi_user_pipeline,
    run_shared_stage,
    run_user_stage,
)
from auspex.pipeline.manifest import (
    complete_step,
    fail_step,
    finalize,
    new_manifest,
    resume_step_index,
    skip_step,
    start_step,
)
from auspex.pipeline.runner import PipelineRunner, run_nightly_pipeline

__all__ = [
    "PipelineContext",
    "PipelineProviders",
    "PipelineRepos",
    "PER_USER_STEPS",
    "SHARED_STEPS",
    "MultiUserRunResult",
    "UserStageResult",
    "run_multi_user_pipeline",
    "run_shared_stage",
    "run_user_stage",
    "complete_step",
    "fail_step",
    "finalize",
    "new_manifest",
    "resume_step_index",
    "skip_step",
    "start_step",
    "PipelineRunner",
    "run_nightly_pipeline",
]
