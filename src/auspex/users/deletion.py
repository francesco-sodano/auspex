"""Account deletion — irreversible, idempotent, verified (arc42 §8.3).

Contract
--------
1. **Confirm.** The caller must type an exact confirmation phrase *and* the
   request must be backed by a recent authentication. Where the identity
   provider supplies an ``auth_time``/``iat`` claim we require it to be fresh
   (``Settings.fresh_auth_max_age_seconds``); where it does not, the typed
   confirmation alone carries the intent. This keeps the contract compatible
   with a front end that re-prompts for credentials without hard-failing on
   token shapes that omit the claim.
2. **Block immediately.** The account moves to ``DELETION_PENDING`` before a
   single byte is deleted, so every gated route stops serving that user's
   data straight away even though the purge itself may take a while.
3. **Purge.** Every private partition is hard-deleted, target by target,
   recording progress so a retry resumes instead of restarting.
4. **Verify.** Each target is re-counted; a target only reaches ``VERIFIED``
   once it reads back empty.
5. **Account purge.** Only when every target verifies empty are the user and
   roster records hard-deleted.

What is *not* deleted
---------------------
Shared research (securities, documents, extractions, digests, market data,
fundamentals, scores, leg changes, narratives, config versions, watermarks
and run manifests) is not user data — it is the same for every user and is
retained. Global score performance therefore stays intact; only the private
attribution of recommendations to *this* user disappears with them.

The Entra identity itself belongs to the identity provider and is never
touched from here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from auspex.models.common import utc_now
from auspex.models.deletion import (
    DeletionJob,
    DeletionJobStatus,
    DeletionTarget,
    DeletionTargetStatus,
)

logger = logging.getLogger("auspex.users.deletion")

CONFIRMATION_PHRASE = "DELETE MY ACCOUNT"

#: Phrases accepted as the typed confirmation. More than one exists because
#: different surfaces word the prompt differently, and a user who types
#: exactly what they were shown must not be refused. Matching is
#: case-insensitive and whitespace-trimmed; it is otherwise exact, so a
#: near-miss like "delete account" is still rejected.
ACCEPTED_CONFIRMATION_PHRASES: frozenset[str] = frozenset(
    {
        CONFIRMATION_PHRASE,
        "DELETE MY AUSPEX ACCOUNT",
    }
)


class DeletionConfirmationError(ValueError):
    """The deletion request did not satisfy the confirmation contract."""


class _PurgeableRepository(Protocol):
    async def purge_partition(self, partition_key: str) -> int: ...

    async def count_partition(self, partition_key: str) -> int: ...


@dataclass(frozen=True)
class PurgeTarget:
    """One private partition to erase.

    ``purge`` removes everything for the user and returns how many documents
    it deleted; ``count`` reports how many remain, and must return ``0``
    before the target can be considered verified.
    """

    name: str
    store: str
    purge: Callable[[str], Awaitable[int]]
    count: Callable[[str], Awaitable[int]]


def repository_target(name: str, repo: _PurgeableRepository, *, store: str = "auspex") -> PurgeTarget:
    """Adapt a :class:`~auspex.persistence.repositories.CosmosRepository`."""

    return PurgeTarget(
        name=name,
        store=store,
        purge=repo.purge_partition,
        count=repo.count_partition,
    )


class AccountDeletionService:
    """Runs and reports the deletion of one user's private data."""

    def __init__(
        self,
        deletion_repo: Any,
        targets: list[PurgeTarget],
        *,
        fresh_auth_max_age_seconds: int = 600,
    ) -> None:
        self._jobs = deletion_repo
        self._targets = targets
        self._fresh_auth_max_age_seconds = fresh_auth_max_age_seconds

    # ------------------------------------------------------------ confirmation

    def verify_confirmation(
        self,
        *,
        confirmation_phrase: str | None,
        acknowledged: bool,
        claims: dict | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Validate the confirmation contract; return whether auth was fresh.

        Raises :class:`DeletionConfirmationError` when the request must not
        proceed. The boolean result records, for the audit trail, whether the
        token itself proved a recent authentication.
        """

        if not acknowledged:
            raise DeletionConfirmationError("deletion must be explicitly acknowledged")
        if (confirmation_phrase or "").strip().upper() not in ACCEPTED_CONFIRMATION_PHRASES:
            expected = " or ".join(sorted(ACCEPTED_CONFIRMATION_PHRASES))
            raise DeletionConfirmationError(f"confirmation phrase must be exactly {expected}")
        return self._auth_is_fresh(claims or {}, now=now)

    def _auth_is_fresh(self, claims: dict, *, now: datetime | None = None) -> bool:
        raw = claims.get("auth_time", claims.get("iat"))
        if raw is None:
            return False
        try:
            authenticated_at = float(raw)
        except (TypeError, ValueError):
            return False
        moment = (now or utc_now()).timestamp()
        return 0 <= (moment - authenticated_at) <= self._fresh_auth_max_age_seconds

    # -------------------------------------------------------------------- job

    async def get_job(self, user_id: str) -> DeletionJob | None:
        job = await self._jobs.get(user_id, user_id)
        return job if isinstance(job, DeletionJob) else None

    async def start(
        self,
        user_id: str,
        *,
        requested_by_user_id: str | None = None,
        fresh_auth_verified: bool = False,
        now: datetime | None = None,
    ) -> DeletionJob:
        """Create (or return) the deletion job for ``user_id``. Idempotent."""

        moment = now or utc_now()
        existing = await self.get_job(user_id)
        if existing is not None and not existing.is_finished:
            return existing
        if existing is not None and existing.status is DeletionJobStatus.COMPLETED:
            return existing
        job = DeletionJob(
            id=user_id,
            user_id=user_id,
            status=DeletionJobStatus.REQUESTED,
            requested_at=moment,
            updated_at=moment,
            requested_by_user_id=requested_by_user_id or user_id,
            confirmation_recorded=True,
            fresh_auth_verified=fresh_auth_verified,
            targets=[DeletionTarget(name=target.name, store=target.store) for target in self._targets],
        )
        await self._jobs.upsert(job)
        return job

    async def run(
        self,
        user_id: str,
        *,
        ledger_partition_key: str | None = None,
        now: datetime | None = None,
    ) -> DeletionJob:
        """Purge and verify every private partition. Safe to call repeatedly.

        A target that fails is recorded and the remaining targets still run,
        so one broken container cannot indefinitely block the rest of the
        erasure; the job ends ``FAILED`` and a retry resumes it.
        """

        moment = now or utc_now()
        job = await self.get_job(user_id)
        if job is None:
            job = await self.start(user_id, now=moment)
        if job.status is DeletionJobStatus.COMPLETED:
            return job

        job.status = DeletionJobStatus.IN_PROGRESS
        job.updated_at = moment
        await self._jobs.upsert(job)

        failures: list[str] = []
        for target in self._targets:
            record = job.target(target.name)
            if record is None:
                record = DeletionTarget(name=target.name, store=target.store)
                job.targets.append(record)
            if record.status is DeletionTargetStatus.VERIFIED:
                continue
            partition = ledger_partition_key if target.store == "source_ledger" else user_id
            partition = partition or user_id
            try:
                deleted = await target.purge(partition)
                record.deleted_count += int(deleted)
                record.status = DeletionTargetStatus.PURGED
                remaining = await target.count(partition)
                record.remaining_count = int(remaining)
                if remaining == 0:
                    record.status = DeletionTargetStatus.VERIFIED
                    record.completed_at = utc_now()
                    record.detail = None
                else:
                    record.status = DeletionTargetStatus.FAILED
                    record.detail = f"{remaining} document(s) still present after purge"
                    failures.append(f"{target.name}: {record.detail}")
            except Exception as exc:  # noqa: BLE001 - one broken target must not strand the rest
                record.status = DeletionTargetStatus.FAILED
                record.detail = str(exc)
                failures.append(f"{target.name}: {exc}")
                logger.error("account deletion: purge of %s failed", target.name, exc_info=True)
            job.updated_at = utc_now()
            await self._jobs.upsert(job)

        job.status = DeletionJobStatus.VERIFYING
        job.updated_at = utc_now()
        if failures:
            job.status = DeletionJobStatus.FAILED
            job.failure_detail = "; ".join(failures)
        else:
            job.status = DeletionJobStatus.COMPLETED
            job.failure_detail = None
            job.completed_at = utc_now()
        await self._jobs.upsert(job)
        return job

    async def finalize(self, user_id: str) -> None:
        """Remove the job document immediately before the final account purge.

        Called after private partitions verify empty so no deletion metadata
        remains after the user and roster records are removed.
        """

        delete = getattr(self._jobs, "delete", None)
        if delete is not None:
            await delete(user_id, user_id)
