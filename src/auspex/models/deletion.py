"""Account deletion job state (`deletion_jobs` container, partition ``/user_id``).

Deletion is a *user-visible, resumable job*, not a single request-scoped
operation: the API flips the user to
:class:`~auspex.models.app_user.UserStatus.DELETION_PENDING` immediately (so
every gated route stops serving data straight away), then hard-deletes each
private partition. Every partition target is tracked separately so a retry
resumes rather than restarting, and so the caller can poll progress.

Shared research data (securities, documents, extractions, digests, scores,
leg changes, market data, fundamentals, narratives, config versions,
watermarks, runs) is explicitly *not* user data and is never touched. The
Entra identity itself is owned by the identity provider and is likewise never
deleted from here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from auspex.models.common import AuspexModel


class DeletionJobStatus(StrEnum):
    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeletionTargetStatus(StrEnum):
    PENDING = "PENDING"
    PURGED = "PURGED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class DeletionTarget(AuspexModel):
    """One private partition scheduled for hard deletion."""

    name: str = Field(description="logical partition name, e.g. 'recommendations'")
    store: str = Field(default="auspex", description="auspex | source_ledger")
    status: DeletionTargetStatus = DeletionTargetStatus.PENDING
    deleted_count: int = 0
    remaining_count: int | None = None
    detail: str | None = None
    completed_at: datetime | None = None


class DeletionJob(AuspexModel):
    """`deletion_jobs` container row, partitioned by ``/user_id``."""

    id: str = Field(description="user_id — one live deletion job per user")
    user_id: str
    status: DeletionJobStatus = DeletionJobStatus.REQUESTED
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    requested_by_user_id: str | None = None
    confirmation_recorded: bool = False
    fresh_auth_verified: bool = False
    targets: list[DeletionTarget] = Field(default_factory=list)
    failure_detail: str | None = None

    @property
    def partition_key(self) -> str:
        return self.user_id

    def target(self, name: str) -> DeletionTarget | None:
        for item in self.targets:
            if item.name == name:
                return item
        return None

    @property
    def progress_pct(self) -> int:
        if not self.targets:
            return 100 if self.status is DeletionJobStatus.COMPLETED else 0
        verified = sum(1 for target in self.targets if target.status is DeletionTargetStatus.VERIFIED)
        return int(verified * 100 / len(self.targets))

    @property
    def is_finished(self) -> bool:
        return self.status in (DeletionJobStatus.COMPLETED, DeletionJobStatus.FAILED)
