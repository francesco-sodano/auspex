"""Account deletion endpoints (arc42 §8.3).

Reachable by any *registered* account, including one already in
``DELETION_PENDING`` — a user must be able to watch their own erasure
finish even though every product route has already stopped serving them.

The flow is deliberately three-legged:

``POST /api/account/deletion``
    Validate the confirmation contract, block the account immediately, then
    purge. Idempotent: re-posting resumes rather than restarting.

``GET /api/account/deletion``
    Progress, per private partition.

``POST /api/account/deletion/resume``
    Re-run a failed or partially completed purge. Exists because deletion
    must eventually succeed even if a container was briefly unavailable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from auspex.api.access import CurrentUser, require_registered_user
from auspex.api.deps import (
    get_app_user_service,
    get_audit_repo,
    get_deletion_job_repo,
    get_onboarding_repo,
    get_portfolio_ledger_service,
    get_portfolio_projection_repo,
    get_recommendation_disposition_repo,
    get_recommendation_repo,
    get_user_performance_repo,
    get_user_settings_repo,
)
from auspex.models.app_user import UserStatus
from auspex.models.deletion import DeletionJob, DeletionJobStatus
from auspex.portfolio.ledger_service import PortfolioLedgerService
from auspex.settings import get_settings
from auspex.users.deletion import (
    AccountDeletionService,
    DeletionConfirmationError,
    PurgeTarget,
    repository_target,
)
from auspex.users.service import AppUserService, LastAdminError, UserLifecycleError

router = APIRouter(prefix="/account/deletion", tags=["account"])


class DeletionRequest(BaseModel):
    """Typed confirmation contract.

    The confirmation phrase must be typed exactly (case-insensitively); both
    ``DELETE MY ACCOUNT`` and ``DELETE MY AUSPEX ACCOUNT`` are accepted, so a
    user who types precisely what the prompt showed them is never refused.
    ``acknowledged`` records that the irreversible consequences were shown;
    clients that send a bare ``confirmation`` string are accepted too, since
    typing the phrase *is* the acknowledgement in that flow.

    Where the identity provider issues an ``auth_time`` claim the API
    additionally requires it to be recent, so a stolen long-lived token
    cannot erase an account on its own — but a missing claim degrades to the
    typed contract rather than locking a legitimate user out of deleting
    their own data.
    """

    model_config = ConfigDict(extra="ignore")

    confirmation_phrase: str | None = Field(
        default=None, validation_alias=AliasChoices("confirmation_phrase", "confirmation")
    )
    acknowledged: bool = False

    @model_validator(mode="after")
    def _typed_phrase_implies_acknowledgement(self) -> DeletionRequest:
        if self.confirmation_phrase and not self.acknowledged:
            self.acknowledged = True
        return self


class DeletionTargetOut(BaseModel):
    name: str
    store: str
    status: str
    deleted_count: int
    remaining_count: int | None = None
    detail: str | None = None


#: Coarse status vocabulary the SPA renders, mapped from the detailed job
#: state so a client does not have to know about purge/verify internals.
CLIENT_STATUS: dict[str, str] = {
    DeletionJobStatus.REQUESTED.value: "PENDING",
    DeletionJobStatus.IN_PROGRESS.value: "RUNNING",
    DeletionJobStatus.VERIFYING.value: "RUNNING",
    DeletionJobStatus.COMPLETED.value: "COMPLETED",
    DeletionJobStatus.FAILED.value: "FAILED",
}


class DeletionStatusOut(BaseModel):
    user_id: str
    status: str
    job_status: str | None = None
    account_status: UserStatus
    progress_pct: int
    requested_at: str | None = None
    completed_at: str | None = None
    fresh_auth_verified: bool = False
    failure_detail: str | None = None
    error: str | None = None
    deleted_items: int = 0
    remaining_items: int = 0
    targets: list[DeletionTargetOut] = []

    @classmethod
    def from_job(cls, job: DeletionJob | None, account_status: UserStatus, user_id: str) -> DeletionStatusOut:
        if job is None:
            return cls(
                user_id=user_id,
                status="NOT_REQUESTED",
                account_status=account_status,
                progress_pct=0,
            )
        return cls(
            user_id=job.user_id,
            status=CLIENT_STATUS.get(job.status.value, job.status.value),
            job_status=job.status.value,
            account_status=account_status,
            progress_pct=job.progress_pct,
            requested_at=job.requested_at.isoformat(),
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            fresh_auth_verified=job.fresh_auth_verified,
            failure_detail=job.failure_detail,
            error=job.failure_detail,
            deleted_items=sum(target.deleted_count for target in job.targets),
            remaining_items=sum(target.remaining_count or 0 for target in job.targets),
            targets=[
                DeletionTargetOut(
                    name=target.name,
                    store=target.store,
                    status=target.status.value,
                    deleted_count=target.deleted_count,
                    remaining_count=target.remaining_count,
                    detail=target.detail,
                )
                for target in job.targets
            ],
        )


def build_purge_targets(
    *,
    ledger: PortfolioLedgerService | None,
    user_id: str,
    settings_repo,
    recommendation_repo,
    disposition_repo,
    projection_repo,
    conversation_repo,
    onboarding_repo,
    audit_repo,
    user_performance_repo,
) -> list[PurgeTarget]:
    """Every private partition belonging to one user.

    Shared research containers (securities, documents, extractions, digests,
    market data, fundamentals, scores, leg changes, narratives, config
    versions, watermarks, runs) are intentionally absent: they are identical
    for every user and are not personal data. Global score performance
    therefore survives the deletion; only this user's private attribution
    disappears.
    """

    targets: list[PurgeTarget] = []
    if ledger is not None:
        targets.append(
            PurgeTarget(
                name="portfolio_transactions",
                store="source_ledger",
                purge=lambda _partition: ledger.purge_owner_ledger(user_id),
                count=lambda _partition: ledger.count_owner_ledger(user_id),
            )
        )
    targets.extend(
        [
            repository_target("user_settings", settings_repo),
            repository_target("recommendations", recommendation_repo),
            repository_target("recommendation_dispositions", disposition_repo),
            repository_target("portfolio_projection", projection_repo),
            repository_target("conversations", conversation_repo),
            repository_target("onboarding", onboarding_repo),
            repository_target("audit_events", audit_repo),
            repository_target("user_performance", user_performance_repo),
        ]
    )
    return targets


def build_deletion_service(
    user_id: str,
    *,
    deletion_repo,
    ledger,
    settings_repo,
    recommendation_repo,
    disposition_repo,
    projection_repo,
    onboarding_repo,
    audit_repo,
    user_performance_repo,
) -> AccountDeletionService:
    """A deletion service covering every private partition of ``user_id``.

    ``user_id`` is the *subject* of the deletion, which is not necessarily the
    caller: an administrator erasing somebody else must address that person's
    partitions, not their own.

    Shared by the self-service route and the administrator route so both paths
    erase exactly the same set — an admin-initiated deletion that only blocked
    the account while leaving the data in place would be an erasure in name
    only.
    """

    from auspex.api.repos import get_conversation_repo

    targets = build_purge_targets(
        ledger=ledger,
        user_id=user_id,
        settings_repo=settings_repo,
        recommendation_repo=recommendation_repo,
        disposition_repo=disposition_repo,
        projection_repo=projection_repo,
        conversation_repo=get_conversation_repo(),
        onboarding_repo=onboarding_repo,
        audit_repo=audit_repo,
        user_performance_repo=user_performance_repo,
    )
    return AccountDeletionService(
        deletion_repo,
        targets,
        fresh_auth_max_age_seconds=get_settings().fresh_auth_max_age_seconds,
    )


def _deletion_service(
    current: CurrentUser,
    deletion_repo,
    ledger,
    settings_repo,
    recommendation_repo,
    disposition_repo,
    projection_repo,
    onboarding_repo,
    audit_repo,
    user_performance_repo,
) -> AccountDeletionService:
    return build_deletion_service(
        current.user_id,
        deletion_repo=deletion_repo,
        ledger=ledger,
        settings_repo=settings_repo,
        recommendation_repo=recommendation_repo,
        disposition_repo=disposition_repo,
        projection_repo=projection_repo,
        onboarding_repo=onboarding_repo,
        audit_repo=audit_repo,
        user_performance_repo=user_performance_repo,
    )


async def _run_and_finalize(
    service: AccountDeletionService,
    users: AppUserService,
    current: CurrentUser,
) -> DeletionJob:
    job = await service.run(current.user_id, ledger_partition_key=current.ledger_partition_key)
    if job.status is DeletionJobStatus.COMPLETED:
        await service.finalize(current.user_id)
        await users.purge_user_record(current.user_id)
    return job


@router.get("", response_model=DeletionStatusOut)
async def get_deletion_status(
    current: CurrentUser = Depends(require_registered_user),
    deletion_repo=Depends(get_deletion_job_repo),
) -> DeletionStatusOut:
    service = AccountDeletionService(deletion_repo, [])
    job = await service.get_job(current.user_id)
    return DeletionStatusOut.from_job(job, current.status, current.user_id)


@router.post("", response_model=DeletionStatusOut, status_code=status.HTTP_202_ACCEPTED)
async def request_deletion(
    request: DeletionRequest,
    current: CurrentUser = Depends(require_registered_user),
    users: AppUserService = Depends(get_app_user_service),
    deletion_repo=Depends(get_deletion_job_repo),
    ledger: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
    settings_repo=Depends(get_user_settings_repo),
    recommendation_repo=Depends(get_recommendation_repo),
    disposition_repo=Depends(get_recommendation_disposition_repo),
    projection_repo=Depends(get_portfolio_projection_repo),
    onboarding_repo=Depends(get_onboarding_repo),
    audit_repo=Depends(get_audit_repo),
    user_performance_repo=Depends(get_user_performance_repo),
) -> DeletionStatusOut:
    service = _deletion_service(
        current,
        deletion_repo,
        ledger,
        settings_repo,
        recommendation_repo,
        disposition_repo,
        projection_repo,
        onboarding_repo,
        audit_repo,
        user_performance_repo,
    )
    try:
        fresh = service.verify_confirmation(
            confirmation_phrase=request.confirmation_phrase,
            acknowledged=request.acknowledged,
            claims=current.claims,
        )
    except DeletionConfirmationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    async with users.user_operation(current.user_id, require_active=False):
        try:
            await users.mark_deletion_pending(
                current.user_id,
                actor_user_id=current.user_id,
            )
        except LastAdminError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": "LAST_ADMIN", "message": str(exc)},
            ) from exc
        except UserLifecycleError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        await service.start(current.user_id, fresh_auth_verified=fresh)
        job = await _run_and_finalize(service, users, current)
    account_status = UserStatus.DELETED if job.status is DeletionJobStatus.COMPLETED else UserStatus.DELETION_PENDING
    return DeletionStatusOut.from_job(job, account_status, current.user_id)


@router.post("/resume", response_model=DeletionStatusOut)
async def resume_deletion(
    current: CurrentUser = Depends(require_registered_user),
    users: AppUserService = Depends(get_app_user_service),
    deletion_repo=Depends(get_deletion_job_repo),
    ledger: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
    settings_repo=Depends(get_user_settings_repo),
    recommendation_repo=Depends(get_recommendation_repo),
    disposition_repo=Depends(get_recommendation_disposition_repo),
    projection_repo=Depends(get_portfolio_projection_repo),
    onboarding_repo=Depends(get_onboarding_repo),
    audit_repo=Depends(get_audit_repo),
    user_performance_repo=Depends(get_user_performance_repo),
) -> DeletionStatusOut:
    """Resume an interrupted purge. Only meaningful once deletion is pending."""

    if current.status is not UserStatus.DELETION_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no deletion is in progress for this account",
        )
    service = _deletion_service(
        current,
        deletion_repo,
        ledger,
        settings_repo,
        recommendation_repo,
        disposition_repo,
        projection_repo,
        onboarding_repo,
        audit_repo,
        user_performance_repo,
    )
    async with users.user_operation(current.user_id, require_active=False):
        job = await _run_and_finalize(service, users, current)
    account_status = UserStatus.DELETED if job.status is DeletionJobStatus.COMPLETED else current.status
    return DeletionStatusOut.from_job(job, account_status, current.user_id)
