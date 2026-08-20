"""Application user lifecycle, roles, and the admin authority binding.

Auspex is multi-user: an Entra External ID principal is authenticated by
:mod:`auspex.api.auth`, but authentication alone grants nothing. Every
``/api`` route is additionally gated on a *database-backed* lifecycle record
in the ``app_users`` container so a valid token for an unknown, pending,
rejected, suspended or deleting principal can read nothing.

Containers
----------
``app_users`` (partition key ``/user_id``)
    The authoritative, private record for one principal. Read by point read
    on the caller's own ``user_id`` — never a cross-partition scan.

``app_user_index`` (partition key ``/scope``)
    A small, admin-visible projection of every user plus the singleton
    admin-authority binding, all inside the single logical partition
    :data:`USER_INDEX_SCOPE`. This exists so the admin roster is a
    partition-local query rather than a cross-partition scan of
    ``app_users``, and so no private user data (settings, portfolio,
    recommendations) is ever co-located with the roster.

Admin authority
---------------
``Settings.initial_admin_email`` names the *first* administrator by email so
a brand-new deployment has someone who can approve anyone else. That email is
only ever consulted while no admin exists yet. The moment the first admin
registers, their immutable Entra ``oid`` is written to
:class:`AdminAuthorityBinding` and authority binds to that object ID forever;
changing the email setting afterwards grants nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from auspex.models.common import AuspexModel

USER_INDEX_SCOPE = "registry"
"""Single logical partition holding the admin roster + authority binding."""

ADMIN_BINDING_ID = "admin_authority_binding"


class UserStatus(StrEnum):
    """Lifecycle of an application user.

    ``UNREGISTERED`` is never persisted — it is the synthetic status reported
    for an authenticated principal that has no ``app_users`` document yet, so
    the session endpoint can tell a first-time visitor to register.
    """

    UNREGISTERED = "UNREGISTERED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED_NEEDS_ONBOARDING = "APPROVED_NEEDS_ONBOARDING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    DELETION_PENDING = "DELETION_PENDING"
    DELETED = "DELETED"


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    USER = "USER"


#: Statuses that may reach product data. Everything else is gated out.
ACTIVE_STATUSES: frozenset[UserStatus] = frozenset({UserStatus.ACTIVE})

#: Statuses that may reach the guided onboarding endpoints.
ONBOARDING_STATUSES: frozenset[UserStatus] = frozenset({UserStatus.APPROVED_NEEDS_ONBOARDING})

#: Statuses from which a user is considered permanently closed.
TERMINAL_STATUSES: frozenset[UserStatus] = frozenset({UserStatus.DELETED})

#: Legal lifecycle transitions. Anything absent here is rejected by the
#: service layer rather than silently applied.
ALLOWED_TRANSITIONS: dict[UserStatus, frozenset[UserStatus]] = {
    UserStatus.PENDING_APPROVAL: frozenset(
        {UserStatus.APPROVED_NEEDS_ONBOARDING, UserStatus.REJECTED, UserStatus.DELETION_PENDING}
    ),
    UserStatus.APPROVED_NEEDS_ONBOARDING: frozenset(
        {UserStatus.ACTIVE, UserStatus.SUSPENDED, UserStatus.REJECTED, UserStatus.DELETION_PENDING}
    ),
    UserStatus.ACTIVE: frozenset({UserStatus.SUSPENDED, UserStatus.DELETION_PENDING}),
    UserStatus.SUSPENDED: frozenset(
        {UserStatus.ACTIVE, UserStatus.APPROVED_NEEDS_ONBOARDING, UserStatus.DELETION_PENDING}
    ),
    UserStatus.REJECTED: frozenset({UserStatus.PENDING_APPROVAL, UserStatus.DELETION_PENDING}),
    UserStatus.DELETION_PENDING: frozenset({UserStatus.DELETED}),
    UserStatus.DELETED: frozenset(),
}


class AppUser(AuspexModel):
    """`app_users` container row, partitioned by ``/user_id``.

    ``user_id`` is the stable surrogate derived from the Entra object ID
    (:func:`auspex.identity.compatible_user_id`) and is used as the partition
    value for *every* private container. ``provider_user_id`` keeps the raw,
    immutable ``oid`` so administrative authority can never be re-pointed by
    changing a mutable attribute such as an email address.
    """

    id: str = Field(description="user_id")
    user_id: str
    provider_user_id: str = Field(description="immutable Entra oid claim")
    identity_provider: str = "aad"
    email: str | None = None
    display_name: str | None = None
    status: UserStatus = UserStatus.PENDING_APPROVAL
    role: UserRole = UserRole.USER
    ledger_partition_key: str = Field(
        description=(
            "partition value used for this user's event ledger. Defaults to user_id; an "
            "imported single-owner ledger may pin a legacy owner_user_sk here so its "
            "existing events stay readable."
        )
    )
    registered_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    approved_by_user_id: str | None = None
    rejected_at: datetime | None = None
    rejected_by_user_id: str | None = None
    suspended_at: datetime | None = None
    suspended_by_user_id: str | None = None
    onboarding_completed_at: datetime | None = None
    activated_at: datetime | None = None
    deletion_requested_at: datetime | None = None
    deleted_at: datetime | None = None
    status_reason: str | None = None
    is_bootstrap_admin: bool = False
    operation_lease_owner: str | None = None
    operation_lease_expires_at: datetime | None = None

    @property
    def partition_key(self) -> str:
        return self.user_id

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN and self.status not in TERMINAL_STATUSES

    def can_access_product(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def can_onboard(self) -> bool:
        return self.status in ONBOARDING_STATUSES


class AppUserSummary(AuspexModel):
    """`app_user_index` roster row, partitioned by ``/scope``.

    Deliberately holds only what an administrator needs in order to approve,
    reject, suspend or promote someone. No settings, portfolio figures,
    recommendations or conversation content ever land here.
    """

    id: str = Field(description="user_id")
    scope: str = USER_INDEX_SCOPE
    kind: str = "user"
    user_id: str
    provider_user_id: str
    email: str | None = None
    display_name: str | None = None
    status: UserStatus
    role: UserRole
    registered_at: datetime
    updated_at: datetime
    onboarding_completed: bool = False
    approved_at: datetime | None = None
    approved_by_user_id: str | None = None

    @property
    def partition_key(self) -> str:
        return self.scope

    @classmethod
    def from_user(cls, user: AppUser) -> AppUserSummary:
        return cls(
            id=user.user_id,
            user_id=user.user_id,
            provider_user_id=user.provider_user_id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
            role=user.role,
            registered_at=user.registered_at,
            updated_at=user.updated_at,
            onboarding_completed=user.onboarding_completed_at is not None,
            approved_at=user.approved_at,
            approved_by_user_id=user.approved_by_user_id,
        )


class AdminAuthorityBinding(AuspexModel):
    """Singleton `app_user_index` row recording the first administrator.

    Written exactly once, when the first administrator registers. From then
    on ``Settings.initial_admin_email`` is inert: authority is bound to
    ``provider_user_id`` (the Entra ``oid``), which the user cannot change.
    """

    id: str = ADMIN_BINDING_ID
    scope: str = USER_INDEX_SCOPE
    kind: str = "admin_binding"
    provider_user_id: str
    user_id: str
    bound_at: datetime
    bootstrap_email: str | None = Field(
        default=None,
        description="the configured initial-admin email that matched at bind time, for audit only",
    )
    mutation_lease_owner: str | None = None
    mutation_lease_expires_at: datetime | None = None

    @property
    def partition_key(self) -> str:
        return self.scope
