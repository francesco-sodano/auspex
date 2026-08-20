"""User-scoped audit trail (`audit_events` container, partition ``/user_id``).

Records lifecycle decisions that affect one user — registration, approval,
rejection, suspension, role changes, onboarding completion and deletion — so
an administrator can explain *why* an account is in its current state.

Because the partition is the subject user, the whole trail is removed with
that user's other private partitions on account deletion. Actions taken *by*
an administrator are additionally recorded under the acting admin's own
partition, so deleting the subject never erases the acting administrator's
own accountability record.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from auspex.models.common import AuspexModel


class AuditEventType(StrEnum):
    REGISTERED = "REGISTERED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    REINSTATED = "REINSTATED"
    ROLE_CHANGED = "ROLE_CHANGED"
    ONBOARDING_COMPLETED = "ONBOARDING_COMPLETED"
    DELETION_REQUESTED = "DELETION_REQUESTED"
    DELETION_COMPLETED = "DELETION_COMPLETED"
    ADMIN_ACTION = "ADMIN_ACTION"


class UserAuditEvent(AuspexModel):
    """`audit_events` container row, partitioned by ``/user_id``."""

    id: str
    user_id: str = Field(description="partition — the account this event is about, or the acting admin")
    subject_user_id: str = Field(description="the account the event concerns")
    event_type: AuditEventType
    actor_user_id: str | None = None
    occurred_at: datetime
    detail: str | None = None
    from_status: str | None = None
    to_status: str | None = None

    @property
    def partition_key(self) -> str:
        return self.user_id
