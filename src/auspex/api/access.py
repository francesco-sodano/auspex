"""Authorisation layer: lifecycle gating on top of Entra authentication.

:mod:`auspex.api.auth` answers *"is this token valid?"*. This module answers
*"is this principal allowed to use Auspex at all, and in what capacity?"* —
which is a database question, not a token question.

Three gates, layered:

``get_app_user``
    Resolves (or synthesises as ``UNREGISTERED``) the caller's lifecycle
    record. Never raises for an unknown user, so the session endpoints can
    tell a first-time visitor what to do next.

``require_active_user``
    The gate applied to the whole ``/api`` product surface. Anything other
    than ``ACTIVE`` is refused with a machine-readable reason so the SPA can
    route to "waiting for approval", "finish onboarding", "suspended", or
    "deleting" without guessing from a bare 403.

``require_admin``
    Additionally requires the ``ADMIN`` role.

Nothing here ever reads another user's private data, and no endpoint accepts
a ``user_id`` from the caller for their own operations: identity always comes
from the validated token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_app_user_service
from auspex.models.app_user import AppUser, UserRole, UserStatus
from auspex.users.service import AppUserService, UserLifecycleError, UserNotFoundError

#: Human-readable reason per non-active status, surfaced to the SPA.
STATUS_DETAIL: dict[UserStatus, str] = {
    UserStatus.UNREGISTERED: "registration required",
    UserStatus.PENDING_APPROVAL: "registration is awaiting administrator approval",
    UserStatus.APPROVED_NEEDS_ONBOARDING: "guided onboarding must be completed first",
    UserStatus.REJECTED: "registration was rejected",
    UserStatus.SUSPENDED: "account is suspended",
    UserStatus.DELETION_PENDING: "account deletion is in progress",
    UserStatus.DELETED: "account has been deleted",
}


@dataclass
class CurrentUser:
    """The authenticated principal together with its lifecycle record."""

    authenticated: AuthenticatedUser
    app_user: AppUser | None

    @property
    def user_id(self) -> str:
        return self.authenticated.user_id

    @property
    def claims(self) -> dict:
        return self.authenticated.claims

    @property
    def status(self) -> UserStatus:
        return self.app_user.status if self.app_user is not None else UserStatus.UNREGISTERED

    @property
    def role(self) -> UserRole:
        return self.app_user.role if self.app_user is not None else UserRole.USER

    @property
    def is_admin(self) -> bool:
        return self.app_user is not None and self.app_user.is_admin

    @property
    def ledger_partition_key(self) -> str:
        if self.app_user is not None:
            return self.app_user.ledger_partition_key
        return self.user_id


def _forbid(status_value: UserStatus) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "reason": status_value.value,
            "message": STATUS_DETAIL.get(status_value, "access denied"),
        },
    )


async def get_app_user(
    user: AuthenticatedUser = Depends(get_current_user),
    service: AppUserService = Depends(get_app_user_service),
) -> CurrentUser:
    """Resolve the caller's lifecycle record without gating on it."""

    return CurrentUser(authenticated=user, app_user=await service.get_user(user.user_id))


async def require_registered_user(current: CurrentUser = Depends(get_app_user)) -> CurrentUser:
    """Any registered, non-terminal account — used by onboarding/deletion."""

    if current.app_user is None:
        raise _forbid(UserStatus.UNREGISTERED)
    if current.status in (UserStatus.DELETED,):
        raise _forbid(current.status)
    return current


async def require_active_user(
    current: CurrentUser = Depends(get_app_user),
    service: AppUserService = Depends(get_app_user_service),
) -> AsyncIterator[CurrentUser]:
    """Gate ACTIVE access and hold the user's durable write/deletion fence."""

    if current.app_user is None or not current.app_user.can_access_product():
        raise _forbid(current.status)
    try:
        async with service.user_operation(
            current.user_id,
            require_active=True,
        ) as app_user:
            yield CurrentUser(
                authenticated=current.authenticated,
                app_user=app_user,
            )
    except (UserLifecycleError, UserNotFoundError) as exc:
        refreshed = await service.get_user(current.user_id)
        raise _forbid(
            refreshed.status if refreshed is not None else UserStatus.DELETED
        ) from exc


async def require_onboarding_user(
    current: CurrentUser = Depends(get_app_user),
    service: AppUserService = Depends(get_app_user_service),
) -> AsyncIterator[CurrentUser]:
    """Gate onboarding and hold the same durable fence deletion acquires."""

    if current.app_user is None:
        raise _forbid(UserStatus.UNREGISTERED)
    if not current.app_user.can_onboard():
        raise _forbid(current.status)
    try:
        async with service.user_operation(
            current.user_id,
            require_active=False,
        ) as app_user:
            if not app_user.can_onboard():
                raise _forbid(app_user.status)
            yield CurrentUser(
                authenticated=current.authenticated,
                app_user=app_user,
            )
    except (UserLifecycleError, UserNotFoundError) as exc:
        refreshed = await service.get_user(current.user_id)
        raise _forbid(
            refreshed.status if refreshed is not None else UserStatus.DELETED
        ) from exc


async def require_admin(current: CurrentUser = Depends(require_active_user)) -> CurrentUser:
    """Administrator-only surface."""

    if not current.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": "NOT_ADMIN", "message": "administrator role required"},
        )
    return current
