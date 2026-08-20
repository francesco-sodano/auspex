"""App user registration, lifecycle transitions, roles and the admin roster.

Design notes
------------
*Point reads, not scans.* A user's own record is a point read on
``app_users`` keyed by their ``user_id``, which is a pure function of the
Entra object ID. The administrator roster is a single-partition query on
``app_user_index`` (partition ``registry``). No request path ever issues a
cross-partition query.

*Two writes, one truth.* ``app_users`` is authoritative; ``app_user_index``
is a derived projection refreshed on every mutation. The projection is
written after the authoritative record, so a crash between the two leaves the
roster stale rather than the record wrong; :meth:`AppUserService.get_user`
always reads the authoritative document.

*Admin authority binds to an object ID.* ``Settings.initial_admin_email``
only matters while :class:`AdminAuthorityBinding` is absent. Once the first
administrator registers, authority is pinned to their immutable ``oid``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Protocol

from azure.core.exceptions import AzureError

from auspex.identity import DEFAULT_IDENTITY_PROVIDER, compatible_user_id
from auspex.models.app_user import (
    ADMIN_BINDING_ID,
    ALLOWED_TRANSITIONS,
    USER_INDEX_SCOPE,
    AdminAuthorityBinding,
    AppUser,
    AppUserSummary,
    UserRole,
    UserStatus,
)
from auspex.models.audit import AuditEventType, UserAuditEvent
from auspex.models.common import new_id, utc_now
from auspex.settings import Settings, get_settings

logger = logging.getLogger("auspex.users.service")

LEASE_SECONDS = 600
LEASE_RENEW_INTERVAL_SECONDS = 60
LEASE_RETRY_SECONDS = 5
LEASE_ACQUIRE_TIMEOUT_SECONDS = 30
_LOCAL_ADMIN_MUTATION_LOCK = asyncio.Lock()
_LOCAL_USER_OPERATION_LOCKS: dict[str, asyncio.Lock] = {}
_HELD_USER_OPERATION_LEASES: ContextVar[frozenset[str]] = ContextVar(
    "auspex_held_user_operation_leases",
    default=frozenset(),
)


class UserNotFoundError(LookupError):
    """No ``app_users`` document exists for the requested user."""


class UserLifecycleError(ValueError):
    """The requested lifecycle transition is not legal from the current state."""


class LastAdminError(UserLifecycleError):
    """Refused: the deployment would be left with no administrator."""


class _Repository(Protocol):
    async def get(self, id_: str, partition_key: str) -> Any: ...

    async def upsert(self, item: Any) -> None: ...

    async def delete(self, id_: str, partition_key: str) -> bool: ...

    async def query(
        self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None
    ) -> list[Any]: ...


class AppUserService:
    """All reads and writes of the application user lifecycle.

    ``user_repo`` is the ``app_users`` container (partition ``/user_id``);
    ``index_repo`` is ``app_user_index`` (partition ``/scope``). ``audit_repo``
    is optional — when absent, lifecycle changes simply are not journalled.
    """

    def __init__(
        self,
        user_repo: _Repository,
        index_repo: _Repository,
        audit_repo: _Repository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._users = user_repo
        self._index = index_repo
        self._audit = audit_repo
        self._settings = settings or get_settings()

    @asynccontextmanager
    async def user_operation(
        self,
        user_id: str,
        *,
        require_active: bool,
    ) -> AsyncIterator[AppUser]:
        """Hold the durable per-user fence used by requests, jobs and deletion."""

        held_leases = _HELD_USER_OPERATION_LEASES.get()
        if user_id in held_leases:
            user = await self.require_user(user_id)
            if require_active and user.status is not UserStatus.ACTIVE:
                raise UserLifecycleError(
                    f"user operation requires ACTIVE status, got {user.status.value}"
                )
            yield user
            return

        get_with_etag = getattr(self._users, "get_with_etag", None)
        replace_if_match = getattr(self._users, "replace_if_match", None)
        if get_with_etag is None or replace_if_match is None:
            lock = _LOCAL_USER_OPERATION_LOCKS.setdefault(user_id, asyncio.Lock())
            async with lock:
                user = await self.require_user(user_id)
                if require_active and user.status is not UserStatus.ACTIVE:
                    raise UserLifecycleError(
                        f"user operation requires ACTIVE status, got {user.status.value}"
                    )
                context_token = _HELD_USER_OPERATION_LEASES.set(
                    held_leases | {user_id}
                )
                try:
                    yield user
                finally:
                    _HELD_USER_OPERATION_LEASES.reset(context_token)
            return

        owner = new_id()
        deadline = time.monotonic() + LEASE_ACQUIRE_TIMEOUT_SECONDS
        while True:
            record = await get_with_etag(user_id, user_id)
            if record is None:
                raise UserNotFoundError(user_id)
            user, etag = record
            if require_active and user.status is not UserStatus.ACTIVE:
                raise UserLifecycleError(
                    f"user operation requires ACTIVE status, got {user.status.value}"
                )
            now = utc_now()
            lease_available = (
                user.operation_lease_owner is None
                or user.operation_lease_expires_at is None
                or user.operation_lease_expires_at <= now
            )
            if lease_available:
                candidate = user.model_copy(deep=True)
                candidate.operation_lease_owner = owner
                candidate.operation_lease_expires_at = now + timedelta(
                    seconds=LEASE_SECONDS
                )
                candidate.updated_at = now
                if await replace_if_match(candidate, etag):
                    remaining = (
                        candidate.operation_lease_expires_at - utc_now()
                    ).total_seconds()
                    if remaining <= 0:
                        continue
                    user = candidate
                    break
            if time.monotonic() >= deadline:
                raise UserLifecycleError("user operation is busy; retry")
            await asyncio.sleep(0.05)

        context_token = _HELD_USER_OPERATION_LEASES.set(
            held_leases | {user_id}
        )
        owner_task = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._renew_user_operation(
                user_id,
                owner,
                owner_task,
                user.operation_lease_expires_at,
            )
        )
        try:
            yield user
        finally:
            try:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                await self._release_user_operation(user_id, owner)
            finally:
                _HELD_USER_OPERATION_LEASES.reset(context_token)

    async def _renew_user_operation(
        self,
        user_id: str,
        owner: str,
        owner_task: asyncio.Task | None,
        confirmed_expiry: datetime,
    ) -> None:
        """Renew a held lease and cancel its work immediately if ownership is lost."""

        try:
            await self._renew_user_operation_loop(
                user_id,
                owner,
                owner_task,
                confirmed_expiry,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "unexpected user operation lease renewal failure for %s",
                user_id,
            )
            self._cancel_lease_owner(
                owner_task,
                user_id,
                "unexpected lease renewal failure",
            )
            raise

    async def _renew_user_operation_loop(
        self,
        user_id: str,
        owner: str,
        owner_task: asyncio.Task | None,
        confirmed_expiry: datetime,
    ) -> None:
        get_with_etag = self._users.get_with_etag
        replace_if_match = self._users.replace_if_match
        confirmed_until = time.monotonic() + max(
            0,
            (confirmed_expiry - utc_now()).total_seconds(),
        )
        while True:
            remaining = confirmed_until - time.monotonic()
            if remaining <= 0:
                self._cancel_lease_owner(
                    owner_task,
                    user_id,
                    "stored lease expired before renewal",
                )
                return
            await asyncio.sleep(
                min(LEASE_RENEW_INTERVAL_SECONDS, remaining)
            )
            while True:
                remaining = confirmed_until - time.monotonic()
                if remaining <= 0:
                    self._cancel_lease_owner(
                        owner_task,
                        user_id,
                        "stored lease expired before renewal",
                    )
                    return
                try:
                    record = await asyncio.wait_for(
                        get_with_etag(user_id, user_id),
                        timeout=remaining,
                    )
                    if record is None:
                        self._cancel_lease_owner(
                            owner_task,
                            user_id,
                            "authoritative user record was removed",
                        )
                        return
                    user, etag = record
                    if user.operation_lease_owner != owner:
                        self._cancel_lease_owner(
                            owner_task,
                            user_id,
                            "lease ownership changed",
                        )
                        return
                    now = utc_now()
                    if user.operation_lease_expires_at is None:
                        self._cancel_lease_owner(
                            owner_task,
                            user_id,
                            "persisted lease expiry was cleared",
                        )
                        return
                    persisted_remaining = (
                        user.operation_lease_expires_at - now
                    ).total_seconds()
                    if persisted_remaining <= 0:
                        self._cancel_lease_owner(
                            owner_task,
                            user_id,
                            "persisted lease expired before renewal",
                        )
                        return
                    confirmed_until = min(
                        confirmed_until,
                        time.monotonic() + persisted_remaining,
                    )
                    user.operation_lease_expires_at = now + timedelta(
                        seconds=LEASE_SECONDS
                    )
                    user.updated_at = now
                    remaining = confirmed_until - time.monotonic()
                    if remaining <= 0:
                        self._cancel_lease_owner(
                            owner_task,
                            user_id,
                            "stored lease expired before conditional renewal",
                        )
                        return
                    replaced = await asyncio.wait_for(
                        replace_if_match(user, etag),
                        timeout=remaining,
                    )
                    if replaced:
                        persisted_remaining = (
                            user.operation_lease_expires_at - utc_now()
                        ).total_seconds()
                        if persisted_remaining <= 0:
                            self._cancel_lease_owner(
                                owner_task,
                                user_id,
                                "renewed lease expired before confirmation",
                            )
                            return
                        confirmed_until = (
                            time.monotonic() + persisted_remaining
                        )
                        break
                except TimeoutError:
                    self._cancel_lease_owner(
                        owner_task,
                        user_id,
                        "lease renewal exceeded the stored expiry",
                    )
                    return
                except AzureError:
                    logger.warning(
                        "could not renew user operation lease for %s; retrying",
                        user_id,
                        exc_info=True,
                    )
                remaining = confirmed_until - time.monotonic()
                if remaining <= 0:
                    self._cancel_lease_owner(
                        owner_task,
                        user_id,
                        "lease renewal could not be confirmed before expiry",
                    )
                    return
                await asyncio.sleep(min(LEASE_RETRY_SECONDS, remaining))

    @staticmethod
    def _cancel_lease_owner(
        owner_task: asyncio.Task | None,
        user_id: str,
        reason: str,
    ) -> None:
        logger.error("user operation lease lost for %s: %s", user_id, reason)
        if owner_task is not None and not owner_task.done():
            owner_task.cancel()

    async def _release_user_operation(self, user_id: str, owner: str) -> None:
        get_with_etag = getattr(self._users, "get_with_etag", None)
        replace_if_match = getattr(self._users, "replace_if_match", None)
        if get_with_etag is None or replace_if_match is None:
            return
        for _ in range(5):
            record = await get_with_etag(user_id, user_id)
            if record is None:
                return
            user, etag = record
            if user.operation_lease_owner != owner:
                return
            user.operation_lease_owner = None
            user.operation_lease_expires_at = None
            user.updated_at = utc_now()
            if await replace_if_match(user, etag):
                return
            await asyncio.sleep(0)
        logger.error("could not release user operation lease for %s; it will expire", user_id)

    @asynccontextmanager
    async def _admin_mutation_guard(self) -> AsyncIterator[None]:
        """Serialize administrator removal through the authority singleton."""

        get_with_etag = getattr(self._index, "get_with_etag", None)
        replace_if_match = getattr(self._index, "replace_if_match", None)
        if get_with_etag is None or replace_if_match is None:
            async with _LOCAL_ADMIN_MUTATION_LOCK:
                yield
            return

        owner = new_id()
        deadline = time.monotonic() + LEASE_ACQUIRE_TIMEOUT_SECONDS
        while True:
            record = await get_with_etag(ADMIN_BINDING_ID, USER_INDEX_SCOPE)
            if record is None:
                async with _LOCAL_ADMIN_MUTATION_LOCK:
                    yield
                return
            binding, etag = record
            now = utc_now()
            lease_available = (
                binding.mutation_lease_owner is None
                or binding.mutation_lease_expires_at is None
                or binding.mutation_lease_expires_at <= now
            )
            if lease_available:
                candidate = binding.model_copy(deep=True)
                candidate.mutation_lease_owner = owner
                candidate.mutation_lease_expires_at = now + timedelta(
                    seconds=LEASE_SECONDS
                )
                if await replace_if_match(candidate, etag):
                    break
            if time.monotonic() >= deadline:
                raise UserLifecycleError("administrator change is busy; retry")
            await asyncio.sleep(0.05)

        try:
            yield
        finally:
            await self._release_admin_mutation(owner)

    async def _release_admin_mutation(self, owner: str) -> None:
        get_with_etag = getattr(self._index, "get_with_etag", None)
        replace_if_match = getattr(self._index, "replace_if_match", None)
        if get_with_etag is None or replace_if_match is None:
            return
        for _ in range(5):
            record = await get_with_etag(ADMIN_BINDING_ID, USER_INDEX_SCOPE)
            if record is None:
                return
            binding, etag = record
            if binding.mutation_lease_owner != owner:
                return
            binding.mutation_lease_owner = None
            binding.mutation_lease_expires_at = None
            if await replace_if_match(binding, etag):
                return
            await asyncio.sleep(0)
        logger.error("could not release administrator mutation lease; it will expire")

    # ------------------------------------------------------------------ reads

    async def get_user(self, user_id: str) -> AppUser | None:
        """Point read of one user's authoritative record."""

        return await self._users.get(user_id, user_id)

    async def require_user(self, user_id: str) -> AppUser:
        user = await self.get_user(user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def list_users(self, *, status: UserStatus | None = None) -> list[AppUserSummary]:
        """Admin roster — a single-partition query, never a scan."""

        if status is None:
            rows = await self._index.query(
                "SELECT * FROM c WHERE c.scope = @scope AND c.kind = @kind",
                [
                    {"name": "@scope", "value": USER_INDEX_SCOPE},
                    {"name": "@kind", "value": "user"},
                ],
                partition_key=USER_INDEX_SCOPE,
            )
        else:
            rows = await self._index.query(
                "SELECT * FROM c WHERE c.scope = @scope AND c.kind = @kind AND c.status = @status",
                [
                    {"name": "@scope", "value": USER_INDEX_SCOPE},
                    {"name": "@kind", "value": "user"},
                    {"name": "@status", "value": status.value},
                ],
                partition_key=USER_INDEX_SCOPE,
            )
        summaries = [row for row in rows if isinstance(row, AppUserSummary)]
        return sorted(summaries, key=lambda item: (item.registered_at, item.user_id))

    async def list_active_user_ids(self) -> list[str]:
        """Every ACTIVE user, for the nightly per-user fan-out."""

        return [summary.user_id for summary in await self.list_users(status=UserStatus.ACTIVE)]

    async def admin_binding(self) -> AdminAuthorityBinding | None:
        binding = await self._index.get(ADMIN_BINDING_ID, USER_INDEX_SCOPE)
        return binding if isinstance(binding, AdminAuthorityBinding) else None

    async def admin_user_ids(self) -> list[str]:
        usable_statuses = {
            UserStatus.APPROVED_NEEDS_ONBOARDING,
            UserStatus.ACTIVE,
        }
        result: list[str] = []
        for summary in await self.list_users():
            if summary.role is not UserRole.ADMIN:
                continue
            user = await self.get_user(summary.user_id)
            if (
                user is not None
                and user.role is UserRole.ADMIN
                and user.status in usable_statuses
            ):
                result.append(user.user_id)
        return result

    async def has_any_admin(self) -> bool:
        return bool(await self.admin_user_ids())

    # ----------------------------------------------------------- registration

    async def register(
        self,
        *,
        provider_user_id: str,
        email: str | None = None,
        email_verified: bool = False,
        display_name: str | None = None,
        identity_provider: str = DEFAULT_IDENTITY_PROVIDER,
        now: datetime | None = None,
    ) -> AppUser:
        """Register an authenticated principal. Idempotent.

        Re-registering an existing user returns the existing record unchanged,
        except that a ``REJECTED`` user is allowed to re-apply (back to
        ``PENDING_APPROVAL``) — a rejection is a decision about an
        application, not a permanent ban.

        The first administrator is bootstrapped here: if no administrator
        exists yet and the caller's verified email matches
        ``Settings.initial_admin_email``, they are created as an ``ADMIN``
        that skips approval, and authority binds to their object ID forever.
        """

        moment = now or utc_now()
        user_id = compatible_user_id(provider_user_id, identity_provider)
        existing = await self.get_user(user_id)
        if existing is not None:
            try:
                async with self.user_operation(
                    user_id,
                    require_active=False,
                ):
                    existing = await self.require_user(user_id)
                    if existing.status in {
                        UserStatus.DELETION_PENDING,
                        UserStatus.DELETED,
                    }:
                        return existing
                    if existing.status is UserStatus.REJECTED:
                        return await self._transition(
                            existing,
                            UserStatus.PENDING_APPROVAL,
                            actor_user_id=user_id,
                            event_type=AuditEventType.REGISTERED,
                            detail="re-applied after rejection",
                            now=moment,
                        )
                    # Refresh mutable profile attributes, never identity or authority.
                    if (email and email != existing.email) or (
                        display_name
                        and display_name != existing.display_name
                    ):
                        existing.email = email or existing.email
                        existing.display_name = (
                            display_name or existing.display_name
                        )
                        existing.updated_at = moment
                        await self._persist(existing)
                    return existing
            except UserNotFoundError as exc:
                raise UserLifecycleError(
                    "account deletion completed while registration was in progress"
                ) from exc

        is_bootstrap_admin = await self._claims_bootstrap_admin(
            provider_user_id=provider_user_id,
            email=email,
            email_verified=email_verified,
        )
        user = AppUser(
            id=user_id,
            user_id=user_id,
            provider_user_id=provider_user_id,
            identity_provider=identity_provider,
            email=email,
            display_name=display_name,
            status=(
                UserStatus.APPROVED_NEEDS_ONBOARDING if is_bootstrap_admin else UserStatus.PENDING_APPROVAL
            ),
            role=UserRole.ADMIN if is_bootstrap_admin else UserRole.USER,
            ledger_partition_key=self._ledger_partition_key(provider_user_id, user_id),
            registered_at=moment,
            updated_at=moment,
            approved_at=moment if is_bootstrap_admin else None,
            approved_by_user_id=user_id if is_bootstrap_admin else None,
            is_bootstrap_admin=is_bootstrap_admin,
            status_reason="initial administrator" if is_bootstrap_admin else None,
        )
        await self._persist(user)
        if is_bootstrap_admin:
            await self._index.upsert(
                AdminAuthorityBinding(
                    provider_user_id=provider_user_id,
                    user_id=user_id,
                    bound_at=moment,
                    bootstrap_email=self._settings.initial_admin_email or None,
                )
            )
        await self._journal(
            subject=user,
            event_type=AuditEventType.REGISTERED,
            actor_user_id=user_id,
            detail="initial administrator" if is_bootstrap_admin else "registration submitted",
            from_status=UserStatus.UNREGISTERED,
            to_status=user.status,
            now=moment,
        )
        return user

    async def _claims_bootstrap_admin(
        self,
        *,
        provider_user_id: str,
        email: str | None,
        email_verified: bool,
    ) -> bool:
        """Whether this principal may claim the initial-administrator seat.

        Authority is granted at most once. If a binding already exists, only
        the bound object ID matches — an attacker who later controls the
        configured email address gains nothing.
        """

        binding = await self.admin_binding()
        if binding is not None:
            return binding.provider_user_id == provider_user_id
        configured_owner = (self._settings.owner_provider_user_id or "").strip()
        if configured_owner and provider_user_id == configured_owner:
            return not await self.has_any_admin()
        configured = (self._settings.initial_admin_email or "").strip().lower()
        trusted_external_email = ".ciamlogin.com" in (
            self._settings.entra_authority or ""
        ).lower()
        if not configured or not (email_verified or trusted_external_email):
            return False
        if (email or "").strip().lower() != configured:
            return False
        return not await self.has_any_admin()

    def _ledger_partition_key(self, provider_user_id: str, user_id: str) -> str:
        """Partition value for this user's event ledger.

        Normally the user's own ``user_id``. A pre-existing single-owner
        deployment may have events stored under a hand-pinned
        ``owner_user_sk``; when the principal registering *is* that
        configured owner, keep the legacy partition so their historical ledger
        stays readable under their own account.
        """

        configured_owner = (self._settings.owner_provider_user_id or "").strip()
        if not configured_owner or configured_owner != provider_user_id:
            return user_id
        override = (self._settings.owner_ledger_partition_key or "").strip()
        if override:
            return override
        legacy = _configured_legacy_owner_partition()
        return legacy or user_id

    # ------------------------------------------------------------ transitions

    async def approve(self, user_id: str, *, actor_user_id: str, now: datetime | None = None) -> AppUser:
        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            return await self._transition(
                user,
                UserStatus.APPROVED_NEEDS_ONBOARDING,
                actor_user_id=actor_user_id,
                event_type=AuditEventType.APPROVED,
                detail="approved by administrator",
                now=now,
            )

    async def reject(
        self, user_id: str, *, actor_user_id: str, reason: str | None = None, now: datetime | None = None
    ) -> AppUser:
        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            return await self._transition_removing_access(
                user,
                UserStatus.REJECTED,
                actor_user_id=actor_user_id,
                event_type=AuditEventType.REJECTED,
                detail=reason or "rejected by administrator",
                now=now,
            )

    async def suspend(
        self, user_id: str, *, actor_user_id: str, reason: str | None = None, now: datetime | None = None
    ) -> AppUser:
        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            return await self._transition_removing_access(
                user,
                UserStatus.SUSPENDED,
                actor_user_id=actor_user_id,
                event_type=AuditEventType.SUSPENDED,
                detail=reason or "suspended by administrator",
                now=now,
            )

    async def reinstate(self, user_id: str, *, actor_user_id: str, now: datetime | None = None) -> AppUser:
        """Lift a suspension.

        A user who had already finished onboarding returns to ``ACTIVE``;
        one suspended mid-onboarding returns to ``APPROVED_NEEDS_ONBOARDING``
        so they finish what they started rather than skipping it.
        """

        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            if user.status not in {UserStatus.SUSPENDED, UserStatus.REJECTED}:
                raise UserLifecycleError(
                    f"cannot reinstate a user in state {user.status.value}"
                )
            target = (
                UserStatus.PENDING_APPROVAL
                if user.status is UserStatus.REJECTED
                else (
                    UserStatus.ACTIVE
                    if user.onboarding_completed_at is not None
                    else UserStatus.APPROVED_NEEDS_ONBOARDING
                )
            )
            return await self._transition(
                user,
                target,
                actor_user_id=actor_user_id,
                event_type=AuditEventType.REINSTATED,
                detail=(
                    "rejection returned to approval queue"
                    if user.status is UserStatus.REJECTED
                    else "suspension lifted"
                ),
                now=now,
            )

    async def complete_onboarding(self, user_id: str, *, now: datetime | None = None) -> AppUser:
        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            moment = now or utc_now()
            user.onboarding_completed_at = moment
            return await self._transition(
                user,
                UserStatus.ACTIVE,
                actor_user_id=user_id,
                event_type=AuditEventType.ONBOARDING_COMPLETED,
                detail="guided onboarding completed",
                now=moment,
            )

    async def mark_deletion_pending(
        self, user_id: str, *, actor_user_id: str, now: datetime | None = None
    ) -> AppUser:
        """Block the account immediately, before any data is touched."""

        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            if user.status is UserStatus.DELETION_PENDING:
                return user
            if user.status is UserStatus.DELETED:
                raise UserLifecycleError("account is already deleted")
            return await self._transition_removing_access(
                user,
                UserStatus.DELETION_PENDING,
                actor_user_id=actor_user_id,
                event_type=AuditEventType.DELETION_REQUESTED,
                detail="account deletion requested",
                now=now,
            )

    async def mark_deleted(self, user_id: str, *, now: datetime | None = None) -> AppUser:
        """Temporarily reduce the record before final hard deletion.

        The deletion route immediately follows this transition with
        :meth:`purge_user_record`; no account tombstone remains.

        This deliberately does **not** journal into the subject's own audit
        partition: that partition has just been purged and verified empty, and
        writing a completion event back into it would leave residue immediately
        before the account record itself is removed.
        """

        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            moment = now or utc_now()
            user.email = None
            user.display_name = None
            user.status_reason = None
            user.deleted_at = moment
            if user.status is UserStatus.DELETED:
                await self._persist(user)
                return user
            return await self._transition(
                user,
                UserStatus.DELETED,
                actor_user_id=user_id,
                event_type=AuditEventType.DELETION_COMPLETED,
                detail="all private partitions purged",
                now=moment,
                journal_subject=False,
            )

    async def purge_user_record(self, user_id: str) -> None:
        """Hard-delete the authoritative account and its admin roster row.

        This is the final account-erasure step, called only after every other
        private partition has been purged and verified. If the deleted user was
        the bootstrap authority, remove that binding too; the last-admin guard
        guarantees another active administrator already exists.
        """

        async with self.user_operation(user_id, require_active=False):
            binding = await self.admin_binding()
            if binding is not None and binding.user_id == user_id:
                async with self._admin_mutation_guard():
                    await self._purge_user_record_unlocked(
                        user_id,
                        await self.admin_binding(),
                    )
                return
            await self._purge_user_record_unlocked(user_id, binding)

    async def _purge_user_record_unlocked(
        self,
        user_id: str,
        binding: AdminAuthorityBinding | None,
    ) -> None:
        await self._index.delete(user_id, USER_INDEX_SCOPE)
        if binding is not None and binding.user_id == user_id:
            remaining_admin_ids = [
                candidate
                for candidate in await self.admin_user_ids()
                if candidate != user_id
            ]
            if not remaining_admin_ids:
                raise LastAdminError(
                    "cannot remove the administrator authority binding "
                    "without a replacement administrator"
                )
            replacement = await self.require_user(sorted(remaining_admin_ids)[0])
            binding.user_id = replacement.user_id
            binding.provider_user_id = replacement.provider_user_id
            await self._index.upsert(binding)
        await self._users.delete(user_id, user_id)

    # ----------------------------------------------------------------- roles

    async def set_role(
        self, user_id: str, role: UserRole, *, actor_user_id: str, now: datetime | None = None
    ) -> AppUser:
        """Promote to ADMIN or demote to USER.

        Demoting the final administrator is refused: a deployment with no
        administrator can never approve anybody again.
        """

        async with self.user_operation(user_id, require_active=False):
            user = await self.require_user(user_id)
            if user.role is UserRole.ADMIN and role is UserRole.USER:
                async with self._admin_mutation_guard():
                    return await self._set_role_unlocked(
                        user_id,
                        role,
                        actor_user_id=actor_user_id,
                        now=now,
                    )
            return await self._set_role_unlocked(
                user_id,
                role,
                actor_user_id=actor_user_id,
                now=now,
            )

    async def _set_role_unlocked(
        self,
        user_id: str,
        role: UserRole,
        *,
        actor_user_id: str,
        now: datetime | None,
    ) -> AppUser:
        user = await self.require_user(user_id)
        if user.role is role:
            return user
        if role is UserRole.ADMIN and user.status not in {
            UserStatus.APPROVED_NEEDS_ONBOARDING,
            UserStatus.ACTIVE,
        }:
            raise UserLifecycleError(
                "only approved users can be promoted to administrator"
            )
        if role is UserRole.USER:
            admins = await self.admin_user_ids()
            if user.user_id in admins and len(admins) <= 1:
                raise LastAdminError("cannot demote the last remaining administrator")
        moment = now or utc_now()
        previous = user.role
        user.role = role
        user.updated_at = moment
        await self._persist(user)
        if previous is UserRole.ADMIN and role is UserRole.USER:
            if not await self.admin_user_ids():
                user.role = previous
                user.updated_at = moment
                await self._persist(user)
                raise LastAdminError("cannot demote the last remaining administrator")
        await self._journal(
            subject=user,
            event_type=AuditEventType.ROLE_CHANGED,
            actor_user_id=actor_user_id,
            detail=f"role {previous.value} -> {role.value}",
            from_status=user.status,
            to_status=user.status,
            now=moment,
        )
        return user

    def _guard_last_admin_removal(self, user: AppUser, admin_user_ids: Sequence[str]) -> None:
        if user.role is not UserRole.ADMIN:
            return
        if user.user_id in admin_user_ids and len(admin_user_ids) <= 1:
            raise LastAdminError("cannot remove the last remaining administrator")

    async def _restore_if_no_admin(self, original: AppUser, result: AppUser) -> AppUser:
        """Rollback a concurrent final-admin removal before reporting success."""

        usable_statuses = {
            UserStatus.APPROVED_NEEDS_ONBOARDING,
            UserStatus.ACTIVE,
        }
        removed_usable_admin = (
            original.role is UserRole.ADMIN
            and original.status in usable_statuses
            and result.status not in usable_statuses
        )
        if removed_usable_admin and not await self.admin_user_ids():
            await self._persist(original)
            raise LastAdminError("cannot remove the last remaining administrator")
        return result

    async def _transition_removing_access(
        self,
        user: AppUser,
        target: UserStatus,
        *,
        actor_user_id: str,
        event_type: AuditEventType,
        detail: str,
        now: datetime | None,
    ) -> AppUser:
        async def apply(current: AppUser) -> AppUser:
            self._guard_last_admin_removal(current, await self.admin_user_ids())
            original = current.model_copy(deep=True)
            moment = now or utc_now()
            if target is UserStatus.DELETION_PENDING:
                current.deletion_requested_at = moment
            result = await self._transition(
                current,
                target,
                actor_user_id=actor_user_id,
                event_type=event_type,
                detail=detail,
                now=moment,
            )
            return await self._restore_if_no_admin(original, result)

        if user.role is UserRole.ADMIN:
            async with self._admin_mutation_guard():
                return await apply(await self.require_user(user.user_id))
        return await apply(user)

    # -------------------------------------------------------------- internals

    async def _transition(
        self,
        user: AppUser,
        target: UserStatus,
        *,
        actor_user_id: str,
        event_type: AuditEventType,
        detail: str | None,
        now: datetime | None = None,
        journal_subject: bool = True,
    ) -> AppUser:
        moment = now or utc_now()
        current = user.status
        if target is not current and target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise UserLifecycleError(f"illegal transition {current.value} -> {target.value}")
        user.status = target
        user.updated_at = moment
        user.status_reason = detail
        if target is UserStatus.APPROVED_NEEDS_ONBOARDING and event_type is AuditEventType.APPROVED:
            user.approved_at = moment
            user.approved_by_user_id = actor_user_id
        if target is UserStatus.REJECTED:
            user.rejected_at = moment
            user.rejected_by_user_id = actor_user_id
        if target is UserStatus.SUSPENDED:
            user.suspended_at = moment
            user.suspended_by_user_id = actor_user_id
        if target is UserStatus.ACTIVE:
            user.activated_at = moment
            user.suspended_at = None
            user.suspended_by_user_id = None
        await self._persist(user)
        await self._journal(
            subject=user,
            event_type=event_type,
            actor_user_id=actor_user_id,
            detail=detail,
            from_status=current,
            to_status=target,
            now=moment,
            journal_subject=journal_subject,
        )
        return user

    async def _persist(self, user: AppUser) -> None:
        await self._users.upsert(user)
        await self._index.upsert(AppUserSummary.from_user(user))

    async def _journal(
        self,
        *,
        subject: AppUser,
        event_type: AuditEventType,
        actor_user_id: str | None,
        detail: str | None,
        from_status: UserStatus | None = None,
        to_status: UserStatus | None = None,
        now: datetime | None = None,
        journal_subject: bool = True,
    ) -> None:
        if self._audit is None:
            return
        moment = now or utc_now()
        if journal_subject:
            await self._audit.upsert(
                UserAuditEvent(
                    id=new_id(),
                    user_id=subject.user_id,
                    subject_user_id=subject.user_id,
                    event_type=event_type,
                    actor_user_id=actor_user_id,
                    occurred_at=moment,
                    detail=detail,
                    from_status=from_status.value if from_status else None,
                    to_status=to_status.value if to_status else None,
                )
            )
        if actor_user_id and actor_user_id != subject.user_id:
            # Mirror administrative actions under the acting admin's own
            # partition so deleting the subject never erases accountability.
            await self._audit.upsert(
                UserAuditEvent(
                    id=new_id(),
                    user_id=actor_user_id,
                    subject_user_id=subject.user_id,
                    event_type=AuditEventType.ADMIN_ACTION,
                    actor_user_id=actor_user_id,
                    occurred_at=moment,
                    detail=f"{event_type.value}: {detail}" if detail else event_type.value,
                    from_status=from_status.value if from_status else None,
                    to_status=to_status.value if to_status else None,
                )
            )


def _configured_legacy_owner_partition() -> str | None:
    """The static ``owner_user_sk`` pinned in ``portfolio_mapping.yaml``, if any.

    Imported deployments may store the pre-existing ledger under a partition
    value that is not the derived ``user_id``. Reading the mapping is
    best-effort: a missing or unreadable file simply means "no legacy
    override", which is the correct answer for a fresh deployment.
    """

    try:
        from auspex.portfolio.mapping import load_portfolio_mapping

        mapping = load_portfolio_mapping()
    except Exception:  # noqa: BLE001 - absence of a mapping is not an error here
        return None
    return mapping.owner_user_sk or None
