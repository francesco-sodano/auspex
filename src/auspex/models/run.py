"""Nightly pipeline run manifest and step checkpoints (`runs` container, arc42 §6.1)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import RunStatus

#: The 20 nightly steps, in execution order.
#:
#: The per-user block is ``PROJECT_PORTFOLIO -> RUN_POLICY -> ASSERT``: the
#: projection is a *precondition* of the policy cascade, not a consequence of
#: it, so the step that produces and persists it runs first and the policy step
#: consumes the cached result. Naming the steps in the order their effects
#: actually occur is what makes the manifest readable as a record of the night.
PIPELINE_STEPS: list[str] = [
    "START_RUN",
    "COLLECT_PRICES",
    "COLLECT_FX",
    "COLLECT_FILINGS",
    "COLLECT_INSIDERS",
    "COLLECT_NEWS",
    "COLLECT_FUNDAMENTALS",
    "EXTRACT_CHANNEL_A",
    "EXTRACT_CHANNEL_B",
    "COMPUTE_RAW_LEGS",
    "ASSIGN_COHORTS",
    "NORMALISE",
    "DIFF",
    "WRITE_SNAPSHOT",
    "PROJECT_PORTFOLIO",
    "RUN_POLICY",
    "ASSERT",
    "NARRATE",
    "VALIDATE",
    "END_RUN",
]


class StepCheckpoint(AuspexModel):
    step: str
    status: str = Field(description="PENDING | RUNNING | SUCCESS | FAILED | SKIPPED")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = None
    degraded: bool = False


class RunManifest(AuspexModel):
    """`runs` container row, partitioned by `/run_date`."""

    id: str = Field(description="{run_date}:{run_type}")
    run_date: date
    run_type: str = Field(default="nightly", description="nightly | performance | bootstrap")
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime
    finished_at: datetime | None = None
    steps: list[StepCheckpoint] = Field(default_factory=list)
    lease_owner: str | None = None
    watermarks_committed: bool = False
    scored_security_count: int | None = None
    hold_insufficient_data_fraction: str | None = None
    degraded_reasons: list[str] = Field(default_factory=list)

    @property
    def partition_key(self) -> str:
        return self.run_date.isoformat()

    def step_by_name(self, name: str) -> StepCheckpoint | None:
        for s in self.steps:
            if s.step == name:
                return s
        return None

    def last_successful_step_index(self) -> int:
        idx = -1
        for i, s in enumerate(self.steps):
            if s.status == "SUCCESS":
                idx = i
            else:
                break
        return idx
