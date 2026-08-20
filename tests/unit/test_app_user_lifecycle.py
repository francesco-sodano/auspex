"""App user lifecycle, roles and admin-authority invariants (arc42 §5.7).

These tests pin the rules that make multi-user Auspex safe:

* authentication alone grants nothing — a registered user starts PENDING;
* the initial-admin *email* is a one-time bootstrap, after which authority is
  bound to an immutable Entra object ID;
* only legal lifecycle transitions are applied;
* the deployment can never be left without an administrator.
"""

from __future__ import annotations

import asyncio
from contextvars import Context

import pytest

from auspex.identity import compatible_user_id
from auspex.models.app_user import UserRole, UserStatus
from auspex.settings import Settings
from auspex.users.service import (
    AppUserService,
    LastAdminError,
    UserLifecycleError,
    UserNotFoundError,
)

from .conftest import InMemoryAppUserRepository

ADMIN_EMAIL = "fsodano79@gmail.com"


class ConditionalRepository(InMemoryAppUserRepository):
    """Cosmos-like point reads and ETag replacement shared by two services."""

    def __init__(self, items=None):
        super().__init__()
        self._versions: dict[tuple[str, str], int] = {}
        self._cas_lock = asyncio.Lock()
        for item in items or []:
            key = (item.id, item.partition_key)
            self.items[key] = item.model_copy(deep=True)
            self._versions[key] = 1

    async def get(self, id_: str, partition_key: str):
        item = self.items.get((id_, partition_key))
        return item.model_copy(deep=True) if item is not None else None

    async def upsert(self, item) -> None:
        async with self._cas_lock:
            key = (item.id, item.partition_key)
            self.items[key] = item.model_copy(deep=True)
            self._versions[key] = self._versions.get(key, 0) + 1

    async def get_with_etag(self, id_: str, partition_key: str):
        async with self._cas_lock:
            key = (id_, partition_key)
            item = self.items.get(key)
            if item is None:
                return None
            return item.model_copy(deep=True), str(self._versions[key])

    async def replace_if_match(self, item, etag: str) -> bool:
        async with self._cas_lock:
            key = (item.id, item.partition_key)
            if str(self._versions.get(key)) != etag:
                return False
            self.items[key] = item.model_copy(deep=True)
            self._versions[key] += 1
            return True


def make_service(*, initial_admin_email: str = ADMIN_EMAIL) -> AppUserService:
    return AppUserService(
        user_repo=InMemoryAppUserRepository(),
        index_repo=InMemoryAppUserRepository(),
        audit_repo=InMemoryAppUserRepository(),
        settings=Settings(
            initial_admin_email=initial_admin_email,
            owner_provider_user_id="oid-admin" if initial_admin_email else "",
        ),
    )


class TestRegistration:
    @pytest.mark.asyncio
    async def test_ordinary_registration_starts_pending_and_not_admin(self):
        service = make_service()

        user = await service.register(provider_user_id="oid-alice", email="alice@example.test")

        assert user.status is UserStatus.PENDING_APPROVAL
        assert user.role is UserRole.USER
        assert user.can_access_product() is False
        assert user.user_id == compatible_user_id("oid-alice")

    @pytest.mark.asyncio
    async def test_registration_is_idempotent(self):
        service = make_service()

        first = await service.register(provider_user_id="oid-alice", email="alice@example.test")
        second = await service.register(provider_user_id="oid-alice", email="alice@example.test")

        assert first.user_id == second.user_id
        assert second.status is UserStatus.PENDING_APPROVAL
        assert len(await service.list_users()) == 1

    @pytest.mark.asyncio
    async def test_ledger_partition_defaults_to_the_users_own_id(self):
        service = make_service()

        user = await service.register(provider_user_id="oid-alice")

        assert user.ledger_partition_key == user.user_id

    @pytest.mark.asyncio
    async def test_rejected_user_may_reapply(self):
        service = make_service()
        admin = await service.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
            email_verified=True,
        )
        alice = await service.register(provider_user_id="oid-alice")
        await service.reject(alice.user_id, actor_user_id=admin.user_id)

        reapplied = await service.register(provider_user_id="oid-alice")

        assert reapplied.status is UserStatus.PENDING_APPROVAL


class TestAdminBootstrap:
    @pytest.mark.asyncio
    async def test_configured_email_bootstraps_the_first_administrator(self):
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=ADMIN_EMAIL),
        )

        admin = await service.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
            email_verified=True,
        )

        assert admin.role is UserRole.ADMIN
        assert admin.is_bootstrap_admin is True
        # Skips the approval queue — there is nobody who could approve them.
        assert admin.status is UserStatus.APPROVED_NEEDS_ONBOARDING

    @pytest.mark.asyncio
    async def test_email_match_is_case_insensitive(self):
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=ADMIN_EMAIL),
        )

        admin = await service.register(
            provider_user_id="oid-admin",
            email="FSodano79@Gmail.COM".lower(),
            email_verified=True,
        )

        assert admin.role is UserRole.ADMIN

    @pytest.mark.asyncio
    async def test_unverified_email_cannot_claim_initial_administrator(self):
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=ADMIN_EMAIL),
        )

        user = await service.register(
            provider_user_id="oid-impostor",
            email=ADMIN_EMAIL,
            email_verified=False,
        )

        assert user.role is UserRole.USER
        assert user.status is UserStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_trusted_external_tenant_email_can_claim_initial_administrator(self):
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(
                initial_admin_email=ADMIN_EMAIL,
                entra_authority="https://auspexfriends.ciamlogin.com/",
            ),
        )

        admin = await service.register(
            provider_user_id="external-oid-admin",
            email=ADMIN_EMAIL,
            email_verified=False,
        )

        assert admin.role is UserRole.ADMIN
        assert admin.status is UserStatus.APPROVED_NEEDS_ONBOARDING

    @pytest.mark.asyncio
    async def test_authority_binds_to_the_object_id_not_the_email(self):
        """Once bound, someone else presenting the same email gains nothing."""

        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)

        impostor = await service.register(provider_user_id="oid-impostor", email=ADMIN_EMAIL)

        assert impostor.role is UserRole.USER
        assert impostor.status is UserStatus.PENDING_APPROVAL
        binding = await service.admin_binding()
        assert binding is not None
        assert binding.provider_user_id == "oid-admin"
        assert binding.user_id == admin.user_id

    @pytest.mark.asyncio
    async def test_no_configured_email_means_no_automatic_admin(self):
        service = make_service(initial_admin_email="")

        user = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)

        assert user.role is UserRole.USER


class TestLifecycleTransitions:
    @pytest.mark.asyncio
    async def test_approval_then_onboarding_reaches_active(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")

        approved = await service.approve(alice.user_id, actor_user_id=admin.user_id)
        assert approved.status is UserStatus.APPROVED_NEEDS_ONBOARDING
        assert approved.can_access_product() is False

        active = await service.complete_onboarding(alice.user_id)
        assert active.status is UserStatus.ACTIVE
        assert active.can_access_product() is True

    @pytest.mark.asyncio
    async def test_illegal_transition_is_refused(self):
        service = make_service()
        alice = await service.register(provider_user_id="oid-alice")

        # PENDING_APPROVAL -> ACTIVE would skip both approval and onboarding.
        with pytest.raises(UserLifecycleError):
            await service.complete_onboarding(alice.user_id)

    @pytest.mark.asyncio
    async def test_suspension_blocks_and_reinstatement_restores(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)
        await service.complete_onboarding(alice.user_id)

        suspended = await service.suspend(alice.user_id, actor_user_id=admin.user_id)
        assert suspended.status is UserStatus.SUSPENDED
        assert suspended.can_access_product() is False

        reinstated = await service.reinstate(alice.user_id, actor_user_id=admin.user_id)
        assert reinstated.status is UserStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_reinstating_mid_onboarding_returns_to_onboarding(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)
        await service.suspend(alice.user_id, actor_user_id=admin.user_id)

        reinstated = await service.reinstate(alice.user_id, actor_user_id=admin.user_id)

        assert reinstated.status is UserStatus.APPROVED_NEEDS_ONBOARDING

    @pytest.mark.asyncio
    async def test_reinstating_rejected_user_returns_to_approval_queue(self):
        service = make_service()
        admin = await service.complete_onboarding(
            (await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)).user_id
        )
        alice = await service.register(provider_user_id="oid-alice")
        await service.reject(alice.user_id, actor_user_id=admin.user_id)

        reinstated = await service.reinstate(alice.user_id, actor_user_id=admin.user_id)

        assert reinstated.status is UserStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_unknown_user_raises(self):
        service = make_service()
        with pytest.raises(UserNotFoundError):
            await service.approve("nobody", actor_user_id="admin")


class TestAdministratorInvariants:
    @pytest.mark.asyncio
    async def test_last_admin_cannot_be_demoted(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)

        with pytest.raises(LastAdminError):
            await service.set_role(admin.user_id, UserRole.USER, actor_user_id=admin.user_id)

    @pytest.mark.asyncio
    async def test_last_admin_cannot_be_suspended_or_deleted(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)

        with pytest.raises(LastAdminError):
            await service.suspend(admin.user_id, actor_user_id=admin.user_id)
        with pytest.raises(LastAdminError):
            await service.mark_deletion_pending(admin.user_id, actor_user_id=admin.user_id)

    @pytest.mark.asyncio
    async def test_demotion_is_allowed_once_a_second_admin_exists(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)
        await service.set_role(alice.user_id, UserRole.ADMIN, actor_user_id=admin.user_id)

        demoted = await service.set_role(admin.user_id, UserRole.USER, actor_user_id=alice.user_id)

        assert demoted.role is UserRole.USER

    @pytest.mark.asyncio
    async def test_suspended_admin_does_not_allow_last_active_admin_removal(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        admin = await service.complete_onboarding(admin.user_id)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)
        alice = await service.complete_onboarding(alice.user_id)
        alice = await service.set_role(
            alice.user_id,
            UserRole.ADMIN,
            actor_user_id=admin.user_id,
        )

        await service.suspend(alice.user_id, actor_user_id=admin.user_id)

        with pytest.raises(LastAdminError):
            await service.suspend(admin.user_id, actor_user_id=admin.user_id)
        assert await service.admin_user_ids() == [admin.user_id]

    @pytest.mark.asyncio
    async def test_promotion_grants_admin_role(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)

        promoted = await service.set_role(alice.user_id, UserRole.ADMIN, actor_user_id=admin.user_id)

        assert promoted.role is UserRole.ADMIN
        assert set(await service.admin_user_ids()) == {admin.user_id, alice.user_id}

    @pytest.mark.asyncio
    async def test_concurrent_admin_suspensions_cannot_remove_every_admin(self):
        class YieldingRepository(InMemoryAppUserRepository):
            async def upsert(self, item) -> None:
                await asyncio.sleep(0)
                await super().upsert(item)

        service = AppUserService(
            user_repo=YieldingRepository(),
            index_repo=YieldingRepository(),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(
                initial_admin_email=ADMIN_EMAIL,
                owner_provider_user_id="oid-admin",
            ),
        )
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id=admin.user_id)
        await service.set_role(alice.user_id, UserRole.ADMIN, actor_user_id=admin.user_id)

        outcomes = await asyncio.gather(
            service.suspend(admin.user_id, actor_user_id=alice.user_id),
            service.suspend(alice.user_id, actor_user_id=admin.user_id),
            return_exceptions=True,
        )

        assert any(isinstance(outcome, LastAdminError) for outcome in outcomes)
        assert len(await service.admin_user_ids()) >= 1

    @pytest.mark.asyncio
    async def test_cross_replica_admin_removals_are_serialized_by_etag_lease(self):
        users = ConditionalRepository()
        index = ConditionalRepository()
        service_one = AppUserService(
            user_repo=users,
            index_repo=index,
            settings=Settings(
                initial_admin_email=ADMIN_EMAIL,
                owner_provider_user_id="oid-admin",
            ),
        )
        service_two = AppUserService(
            user_repo=users,
            index_repo=index,
            settings=Settings(
                initial_admin_email=ADMIN_EMAIL,
                owner_provider_user_id="oid-admin",
            ),
        )
        admin = await service_one.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
        )
        alice = await service_one.register(provider_user_id="oid-alice")
        await service_one.approve(alice.user_id, actor_user_id=admin.user_id)
        await service_one.set_role(
            alice.user_id,
            UserRole.ADMIN,
            actor_user_id=admin.user_id,
        )

        outcomes = await asyncio.gather(
            service_one.suspend(admin.user_id, actor_user_id=alice.user_id),
            service_two.suspend(alice.user_id, actor_user_id=admin.user_id),
            return_exceptions=True,
        )

        assert any(isinstance(outcome, LastAdminError) for outcome in outcomes)
        assert len(await service_one.admin_user_ids()) == 1


class TestDurableUserOperationFence:
    @pytest.mark.asyncio
    async def test_heartbeat_honors_a_shorter_persisted_expiry(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr("auspex.users.service.LEASE_SECONDS", 0.12)
        monkeypatch.setattr(
            "auspex.users.service.LEASE_RENEW_INTERVAL_SECONDS",
            0.01,
        )

        class ControlledRenewalRepository(ConditionalRepository):
            def __init__(self):
                super().__init__()
                self.delay_renewals = False

            async def replace_if_match(self, item, etag: str) -> bool:
                current = self.items.get((item.id, item.partition_key))
                is_renewal = (
                    current is not None
                    and current.operation_lease_owner is not None
                    and current.operation_lease_owner
                    == item.operation_lease_owner
                    and item.operation_lease_expires_at is not None
                )
                if self.delay_renewals and is_renewal:
                    await asyncio.sleep(0.3)
                return await super().replace_if_match(item, etag)

        users = ControlledRenewalRepository()
        index = ConditionalRepository()
        service = AppUserService(
            users,
            index,
            settings=Settings(initial_admin_email=""),
        )
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id="test-admin")
        alice = await service.complete_onboarding(alice.user_id)
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def fenced_work():
            try:
                async with service.user_operation(
                    alice.user_id,
                    require_active=True,
                ):
                    entered.set()
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(fenced_work(), context=Context())
        await entered.wait()
        stale = await users.get(alice.user_id, alice.user_id)
        for _ in range(20):
            current = await users.get(alice.user_id, alice.user_id)
            if (
                current.operation_lease_expires_at
                > stale.operation_lease_expires_at
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("heartbeat did not extend the initial lease")

        users.delay_renewals = True
        await users.upsert(stale)

        await asyncio.wait_for(cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_renewal_cancels_before_the_persisted_expiry(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr("auspex.users.service.LEASE_SECONDS", 0.08)
        monkeypatch.setattr(
            "auspex.users.service.LEASE_RENEW_INTERVAL_SECONDS",
            0.01,
        )

        class SlowRenewalRepository(ConditionalRepository):
            def __init__(self):
                super().__init__()
                self.delay_renewals = False

            async def replace_if_match(self, item, etag: str) -> bool:
                current = self.items.get((item.id, item.partition_key))
                is_renewal = (
                    current is not None
                    and current.operation_lease_owner is not None
                    and current.operation_lease_owner
                    == item.operation_lease_owner
                    and item.operation_lease_expires_at is not None
                )
                if self.delay_renewals and is_renewal:
                    await asyncio.sleep(0.2)
                return await super().replace_if_match(item, etag)

        users = SlowRenewalRepository()
        index = ConditionalRepository()
        service = AppUserService(
            users,
            index,
            settings=Settings(initial_admin_email=""),
        )
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id="test-admin")
        alice = await service.complete_onboarding(alice.user_id)
        users.delay_renewals = True
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def fenced_work():
            try:
                async with service.user_operation(
                    alice.user_id,
                    require_active=True,
                ):
                    entered.set()
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(fenced_work(), context=Context())
        await entered.wait()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_lease_heartbeat_cancels_work_when_ownership_is_lost(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "auspex.users.service.LEASE_RENEW_INTERVAL_SECONDS",
            0.01,
        )
        users = ConditionalRepository()
        index = ConditionalRepository()
        service = AppUserService(
            users,
            index,
            settings=Settings(initial_admin_email=""),
        )
        alice = await service.register(provider_user_id="oid-alice")
        await service.approve(alice.user_id, actor_user_id="test-admin")
        alice = await service.complete_onboarding(alice.user_id)
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def fenced_work():
            try:
                async with service.user_operation(
                    alice.user_id,
                    require_active=True,
                ):
                    entered.set()
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(fenced_work(), context=Context())
        await entered.wait()
        stolen = await users.get(alice.user_id, alice.user_id)
        stolen.operation_lease_owner = "different-replica"
        await users.upsert(stolen)

        await asyncio.wait_for(cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_deletion_waits_for_an_inflight_writer_across_replicas(self):
        users = ConditionalRepository()
        index = ConditionalRepository()
        settings = Settings(
            initial_admin_email=ADMIN_EMAIL,
            owner_provider_user_id="oid-admin",
        )
        writer_service = AppUserService(users, index, settings=settings)
        deletion_service = AppUserService(users, index, settings=settings)
        admin = await writer_service.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
        )
        alice = await writer_service.register(provider_user_id="oid-alice")
        await writer_service.approve(
            alice.user_id,
            actor_user_id=admin.user_id,
        )
        alice = await writer_service.complete_onboarding(alice.user_id)

        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        deletion_entered = asyncio.Event()

        async def writer():
            async with writer_service.user_operation(
                alice.user_id,
                require_active=True,
            ):
                writer_entered.set()
                await release_writer.wait()

        async def delete():
            await writer_entered.wait()
            async with deletion_service.user_operation(
                alice.user_id,
                require_active=False,
            ):
                deletion_entered.set()
                await deletion_service.mark_deletion_pending(
                    alice.user_id,
                    actor_user_id=alice.user_id,
                )

        writer_task = asyncio.create_task(writer())
        deletion_task = asyncio.create_task(delete())
        await writer_entered.wait()
        await asyncio.sleep(0.1)
        assert deletion_entered.is_set() is False

        release_writer.set()
        await asyncio.gather(writer_task, deletion_task)

        assert deletion_entered.is_set() is True
        assert (await deletion_service.require_user(alice.user_id)).status is (
            UserStatus.DELETION_PENDING
        )

    @pytest.mark.asyncio
    async def test_admin_mutation_waits_for_the_target_users_request(self):
        users = ConditionalRepository()
        index = ConditionalRepository()
        settings = Settings(
            initial_admin_email=ADMIN_EMAIL,
            owner_provider_user_id="oid-admin",
        )
        request_service = AppUserService(users, index, settings=settings)
        admin_service = AppUserService(users, index, settings=settings)
        admin = await request_service.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
        )
        alice = await request_service.register(provider_user_id="oid-alice")
        await request_service.approve(
            alice.user_id,
            actor_user_id=admin.user_id,
        )
        alice = await request_service.complete_onboarding(alice.user_id)

        async with request_service.user_operation(
            alice.user_id,
            require_active=True,
        ):
            suspension = asyncio.create_task(
                admin_service.suspend(
                    alice.user_id,
                    actor_user_id=admin.user_id,
                ),
                context=Context(),
            )
            await asyncio.sleep(0.1)
            assert suspension.done() is False

        assert (await suspension).status is UserStatus.SUSPENDED

    @pytest.mark.asyncio
    async def test_registration_cannot_overwrite_deletion_pending(self):
        users = ConditionalRepository()
        index = ConditionalRepository()
        settings = Settings(
            initial_admin_email=ADMIN_EMAIL,
            owner_provider_user_id="oid-admin",
        )
        deletion_service = AppUserService(users, index, settings=settings)
        registration_service = AppUserService(users, index, settings=settings)
        admin = await deletion_service.register(
            provider_user_id="oid-admin",
            email=ADMIN_EMAIL,
        )
        alice = await deletion_service.register(
            provider_user_id="oid-alice",
            email="original@example.test",
        )
        await deletion_service.approve(
            alice.user_id,
            actor_user_id=admin.user_id,
        )
        alice = await deletion_service.complete_onboarding(alice.user_id)

        async with deletion_service.user_operation(
            alice.user_id,
            require_active=False,
        ):
            registration = asyncio.create_task(
                registration_service.register(
                    provider_user_id="oid-alice",
                    email="stale-update@example.test",
                ),
                context=Context(),
            )
            await asyncio.sleep(0.1)
            assert registration.done() is False
            await deletion_service.mark_deletion_pending(
                alice.user_id,
                actor_user_id=alice.user_id,
            )

        result = await registration
        assert result.status is UserStatus.DELETION_PENDING
        assert result.email == "original@example.test"


class TestRoster:
    @pytest.mark.asyncio
    async def test_active_user_ids_drive_the_nightly_fan_out(self):
        service = make_service()
        admin = await service.register(provider_user_id="oid-admin", email=ADMIN_EMAIL)
        alice = await service.register(provider_user_id="oid-alice")
        bob = await service.register(provider_user_id="oid-bob")
        for user in (alice, bob):
            await service.approve(user.user_id, actor_user_id=admin.user_id)
        await service.complete_onboarding(alice.user_id)

        active = await service.list_active_user_ids()

        # Only the fully onboarded user is nightly-eligible.
        assert active == [alice.user_id]
        assert admin.user_id not in active
        assert bob.user_id not in active

    @pytest.mark.asyncio
    async def test_roster_carries_no_private_product_data(self):
        service = make_service()
        await service.register(provider_user_id="oid-alice", email="alice@example.test")

        summary = (await service.list_users())[0]
        payload = summary.model_dump(mode="json")

        for private in ("cash_reserve_chf", "risk_profile", "positions", "recommendations", "ledger_partition_key"):
            assert private not in payload
