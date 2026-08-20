"""Administrator surface: approve, reject, suspend, reinstate, set roles.

Scope discipline
----------------
An administrator manages *access*, not *data*. Every response here is built
from :class:`~auspex.models.app_user.AppUserSummary`, the roster projection
that deliberately contains no settings, portfolio figures, recommendations,
conversations or ledger events. There is no endpoint anywhere in Auspex that
lets one user read another user's private partitions — administrators
included.

Invariants enforced
-------------------
* Only legal lifecycle transitions are applied (see ``ALLOWED_TRANSITIONS``).
* The final administrator can never be demoted, rejected, suspended or
  deleted — a deployment with no administrator could never approve anyone
  again.
* Administrators cannot suspend or delete themselves into that same corner;
  the last-admin guard applies regardless of who is asking.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from auspex.api.access import CurrentUser, require_admin
from auspex.api.deps import (
    get_app_user_service,
    get_audit_repo,
    get_deletion_job_repo,
    get_ledger_service_builder,
    get_onboarding_repo,
    get_portfolio_projection_repo,
    get_recommendation_disposition_repo,
    get_recommendation_repo,
    get_user_performance_repo,
    get_user_settings_repo,
)
from auspex.api.routes.account_deletion import build_deletion_service
from auspex.models.app_user import AppUser, AppUserSummary, UserRole, UserStatus
from auspex.models.deletion import DeletionJobStatus
from auspex.users.service import (
    AppUserService,
    LastAdminError,
    UserLifecycleError,
    UserNotFoundError,
)

router = APIRouter(prefix="/admin/users", tags=["admin"])


class AdminUserOut(BaseModel):
    """Exactly the non-private attributes an administrator needs."""

    user_id: str
    email: str | None = None
    display_name: str | None = None
    status: UserStatus
    role: UserRole
    registered_at: str
    updated_at: str
    created_at: str
    onboarding_completed: bool = False
    approved_at: str | None = None
    approved_by: str | None = None

    @classmethod
    def from_summary(cls, summary: AppUserSummary) -> AdminUserOut:
        return cls(
            user_id=summary.user_id,
            email=summary.email,
            display_name=summary.display_name,
            status=summary.status,
            role=summary.role,
            registered_at=summary.registered_at.isoformat(),
            updated_at=summary.updated_at.isoformat(),
            created_at=summary.registered_at.isoformat(),
            onboarding_completed=summary.onboarding_completed,
            approved_at=summary.approved_at.isoformat() if summary.approved_at else None,
            approved_by=summary.approved_by_user_id,
        )

    @classmethod
    def from_user(cls, user: AppUser) -> AdminUserOut:
        return cls.from_summary(AppUserSummary.from_user(user))


class ReasonRequest(BaseModel):
    reason: str | None = None


class RoleRequest(BaseModel):
    role: UserRole


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, UserNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if isinstance(exc, LastAdminError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "LAST_ADMIN", "message": str(exc)},
        )
    if isinstance(exc, UserLifecycleError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc


@router.get("", response_model=list[AdminUserOut])
async def list_users(
    status_filter: UserStatus | None = Query(
        default=None,
        alias="status",
        description="restrict the roster to one lifecycle state",
    ),
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> list[AdminUserOut]:
    """Roster of every registered account (single-partition query)."""

    summaries = await service.list_users(status=status_filter)
    return [AdminUserOut.from_summary(summary) for summary in summaries]


@router.get("/{user_id}", response_model=AdminUserOut)
async def get_user(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    try:
        user = await service.require_user(user_id)
    except UserNotFoundError as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.post("/{user_id}/approve", response_model=AdminUserOut)
async def approve_user(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    try:
        user = await service.approve(user_id, actor_user_id=admin.user_id)
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.post("/{user_id}/reject", response_model=AdminUserOut)
async def reject_user(
    user_id: str,
    request: ReasonRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    try:
        user = await service.reject(
            user_id, actor_user_id=admin.user_id, reason=(request.reason if request else None)
        )
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.post("/{user_id}/suspend", response_model=AdminUserOut)
async def suspend_user(
    user_id: str,
    request: ReasonRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    try:
        user = await service.suspend(
            user_id, actor_user_id=admin.user_id, reason=(request.reason if request else None)
        )
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.post("/{user_id}/reinstate", response_model=AdminUserOut)
async def reinstate_user(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    try:
        user = await service.reinstate(user_id, actor_user_id=admin.user_id)
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.post("/{user_id}/role", response_model=AdminUserOut)
async def set_user_role(
    user_id: str,
    request: RoleRequest,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    """Promote to ADMIN or demote to USER. Demoting the last admin is refused."""

    try:
        user = await service.set_role(user_id, request.role, actor_user_id=admin.user_id)
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)


@router.put("/{user_id}/role", response_model=AdminUserOut)
async def replace_user_role(
    user_id: str,
    request: RoleRequest,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
) -> AdminUserOut:
    """``PUT`` spelling of :func:`set_user_role`.

    Declared as its own operation rather than a multi-method route so each
    verb keeps a unique OpenAPI ``operationId`` — a duplicate id silently
    breaks generated API clients.
    """

    return await set_user_role(user_id, request, admin, service)


@router.delete("/{user_id}", response_model=AdminUserOut)
async def schedule_user_deletion(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    service: AppUserService = Depends(get_app_user_service),
    deletion_repo=Depends(get_deletion_job_repo),
    settings_repo=Depends(get_user_settings_repo),
    recommendation_repo=Depends(get_recommendation_repo),
    disposition_repo=Depends(get_recommendation_disposition_repo),
    projection_repo=Depends(get_portfolio_projection_repo),
    onboarding_repo=Depends(get_onboarding_repo),
    audit_repo=Depends(get_audit_repo),
    user_performance_repo=Depends(get_user_performance_repo),
    ledger_builder=Depends(get_ledger_service_builder),
) -> AdminUserOut:
    """Block an account and erase it.

    The account stops serving data immediately, then every private partition
    is purged and verified exactly as it is for a self-service deletion — the
    target user is not required to sign in again for their data to actually
    disappear. The last-admin guard applies here too.

    The ledger binding is built for the *subject*, not the acting
    administrator, so the purge addresses the subject's own partition and
    nobody else's.
    """

    lease = service.user_operation(user_id, require_active=False)
    try:
        async with lease:
            user = await service.mark_deletion_pending(
                user_id,
                actor_user_id=admin.user_id,
            )

            deletion_service = build_deletion_service(
                user.user_id,
                deletion_repo=deletion_repo,
                ledger=ledger_builder(user.user_id, user.ledger_partition_key),
                settings_repo=settings_repo,
                recommendation_repo=recommendation_repo,
                disposition_repo=disposition_repo,
                projection_repo=projection_repo,
                onboarding_repo=onboarding_repo,
                audit_repo=audit_repo,
                user_performance_repo=user_performance_repo,
            )
            await deletion_service.start(
                user.user_id,
                requested_by_user_id=admin.user_id,
            )
            job = await deletion_service.run(
                user.user_id,
                ledger_partition_key=user.ledger_partition_key,
            )
            if job.status is DeletionJobStatus.COMPLETED:
                await deletion_service.finalize(user.user_id)
                await service.purge_user_record(user.user_id)
                user.status = UserStatus.DELETED
                user.email = None
                user.display_name = None
                user.status_reason = None
    except (UserNotFoundError, UserLifecycleError) as exc:
        raise _translate(exc) from exc
    return AdminUserOut.from_user(user)
