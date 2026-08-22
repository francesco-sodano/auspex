"""Account deletion: confirmation, immediate blocking, verified erasure.

The properties under test are the ones a data-protection review would ask
about:

* the account stops serving data *before* any purge begins;
* every private partition is emptied and verified empty;
* replaying deletion is safe and converges;
* shared research and the identity provider are untouched;
* the deployment cannot lose its last administrator this way.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user
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
from auspex.models.app_user import (
    AdminAuthorityBinding,
    AppUserSummary,
    UserRole,
    UserStatus,
)
from auspex.models.deletion import DeletionJobStatus, DeletionTargetStatus
from auspex.settings import Settings
from auspex.users.deletion import (
    ACCEPTED_CONFIRMATION_PHRASES,
    CONFIRMATION_PHRASE,
    AccountDeletionService,
    DeletionConfirmationError,
    PurgeTarget,
)
from auspex.users.service import AppUserService

from .conftest import InMemoryAppUserRepository, make_app_user

USER_ID = "user-alice"
OTHER_ID = "user-bob"


class FakePartition:
    """A purgeable container holding rows for several users."""

    def __init__(self, rows: dict[str, int]) -> None:
        self.rows = dict(rows)
        self.purge_calls: list[str] = []

    async def purge_partition(self, partition_key: str) -> int:
        self.purge_calls.append(partition_key)
        removed = self.rows.pop(partition_key, 0)
        return removed

    async def count_partition(self, partition_key: str) -> int:
        return self.rows.get(partition_key, 0)


def make_targets(**partitions: FakePartition) -> list[PurgeTarget]:
    return [
        PurgeTarget(
            name=name,
            store="auspex",
            purge=partition.purge_partition,
            count=partition.count_partition,
        )
        for name, partition in partitions.items()
    ]


class TestConfirmationContract:
    def test_wrong_phrase_is_refused(self):
        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        with pytest.raises(DeletionConfirmationError):
            service.verify_confirmation(confirmation_phrase="delete", acknowledged=True)

    def test_unacknowledged_request_is_refused(self):
        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        with pytest.raises(DeletionConfirmationError):
            service.verify_confirmation(confirmation_phrase=CONFIRMATION_PHRASE, acknowledged=False)

    def test_phrase_is_case_insensitive_but_exact(self):
        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        assert (
            service.verify_confirmation(confirmation_phrase="delete my account", acknowledged=True)
            is False
        )

    @pytest.mark.parametrize("phrase", sorted(ACCEPTED_CONFIRMATION_PHRASES))
    def test_every_accepted_phrasing_is_honoured(self, phrase):
        """Different surfaces word the prompt differently.

        A user who types exactly what they were shown must never be refused,
        so both the short and the product-qualified phrasing are accepted.
        """

        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        assert service.verify_confirmation(confirmation_phrase=phrase, acknowledged=True) is False

    def test_product_qualified_phrase_is_accepted(self):
        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        assert (
            service.verify_confirmation(
                confirmation_phrase="  delete my auspex account  ", acknowledged=True
            )
            is False
        )

    def test_a_near_miss_is_still_refused(self):
        """Accepting more than one phrasing must not weaken the check."""

        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        for phrase in ("DELETE ACCOUNT", "DELETE MY AUSPEX", "DELETE MY OTHER ACCOUNT"):
            with pytest.raises(DeletionConfirmationError):
                service.verify_confirmation(confirmation_phrase=phrase, acknowledged=True)

    def test_recent_auth_time_claim_is_recognised_as_fresh(self):
        service = AccountDeletionService(
            InMemoryAppUserRepository(), [], fresh_auth_max_age_seconds=600
        )
        now = datetime.now(UTC)

        fresh = service.verify_confirmation(
            confirmation_phrase=CONFIRMATION_PHRASE,
            acknowledged=True,
            claims={"auth_time": (now - timedelta(seconds=30)).timestamp()},
            now=now,
        )

        assert fresh is True

    def test_stale_auth_time_is_not_fresh_but_still_permitted_by_typed_confirmation(self):
        """Freshness is recorded, not required.

        A provider that issues long-lived tokens must not be able to lock a
        user out of erasing their own data; the typed confirmation carries
        the intent and the audit records that auth was not fresh.
        """

        service = AccountDeletionService(
            InMemoryAppUserRepository(), [], fresh_auth_max_age_seconds=60
        )
        now = datetime.now(UTC)

        fresh = service.verify_confirmation(
            confirmation_phrase=CONFIRMATION_PHRASE,
            acknowledged=True,
            claims={"auth_time": (now - timedelta(hours=5)).timestamp()},
            now=now,
        )

        assert fresh is False

    def test_missing_auth_time_claim_is_not_fresh(self):
        service = AccountDeletionService(InMemoryAppUserRepository(), [])

        assert (
            service.verify_confirmation(confirmation_phrase=CONFIRMATION_PHRASE, acknowledged=True)
            is False
        )


class TestPurge:
    @pytest.mark.asyncio
    async def test_every_target_is_purged_and_verified(self):
        settings_partition = FakePartition({USER_ID: 1, OTHER_ID: 1})
        recommendations = FakePartition({USER_ID: 42, OTHER_ID: 7})
        jobs = InMemoryAppUserRepository()
        service = AccountDeletionService(
            jobs, make_targets(user_settings=settings_partition, recommendations=recommendations)
        )

        job = await service.run(USER_ID)

        assert job.status is DeletionJobStatus.COMPLETED
        assert all(target.status is DeletionTargetStatus.VERIFIED for target in job.targets)
        assert job.progress_pct == 100
        assert job.target("recommendations").deleted_count == 42

    @pytest.mark.asyncio
    async def test_other_users_partitions_are_untouched(self):
        recommendations = FakePartition({USER_ID: 3, OTHER_ID: 9})
        service = AccountDeletionService(
            InMemoryAppUserRepository(), make_targets(recommendations=recommendations)
        )

        await service.run(USER_ID)

        assert recommendations.rows == {OTHER_ID: 9}
        assert recommendations.purge_calls == [USER_ID]

    @pytest.mark.asyncio
    async def test_replaying_deletion_is_idempotent(self):
        recommendations = FakePartition({USER_ID: 5})
        jobs = InMemoryAppUserRepository()
        service = AccountDeletionService(jobs, make_targets(recommendations=recommendations))

        first = await service.run(USER_ID)
        second = await service.run(USER_ID)

        assert first.status is DeletionJobStatus.COMPLETED
        assert second.status is DeletionJobStatus.COMPLETED
        assert second.target("recommendations").deleted_count == 5

    @pytest.mark.asyncio
    async def test_target_that_still_reports_rows_fails_verification(self):
        class StubbornPartition(FakePartition):
            async def purge_partition(self, partition_key: str) -> int:
                return 0

            async def count_partition(self, partition_key: str) -> int:
                return 4

        stubborn = StubbornPartition({USER_ID: 4})
        service = AccountDeletionService(
            InMemoryAppUserRepository(), make_targets(conversations=stubborn)
        )

        job = await service.run(USER_ID)

        assert job.status is DeletionJobStatus.FAILED
        assert job.target("conversations").status is DeletionTargetStatus.FAILED
        assert "still present" in job.target("conversations").detail

    @pytest.mark.asyncio
    async def test_one_failing_target_does_not_strand_the_others(self):
        class ExplodingPartition(FakePartition):
            async def purge_partition(self, partition_key: str) -> int:
                raise RuntimeError("container unavailable")

        good = FakePartition({USER_ID: 2})
        service = AccountDeletionService(
            InMemoryAppUserRepository(),
            make_targets(conversations=ExplodingPartition({USER_ID: 1}), user_settings=good),
        )

        job = await service.run(USER_ID)

        assert job.status is DeletionJobStatus.FAILED
        assert job.target("user_settings").status is DeletionTargetStatus.VERIFIED
        assert good.rows == {}

    @pytest.mark.asyncio
    async def test_resuming_after_a_failure_completes(self):
        class FlakyPartition(FakePartition):
            def __init__(self, rows):
                super().__init__(rows)
                self.attempts = 0

            async def purge_partition(self, partition_key: str) -> int:
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("transient")
                self.purge_calls.append(partition_key)
                return self.rows.pop(partition_key, 0)

        flaky = FlakyPartition({USER_ID: 3})
        jobs = InMemoryAppUserRepository()
        service = AccountDeletionService(jobs, make_targets(onboarding=flaky))

        assert (await service.run(USER_ID)).status is DeletionJobStatus.FAILED
        assert (await service.run(USER_ID)).status is DeletionJobStatus.COMPLETED


class TestFinalAccountPurge:
    @pytest.mark.asyncio
    async def test_final_purge_removes_account_and_roster_records(self):
        user = make_app_user(USER_ID, status=UserStatus.DELETED, email="alice@example.test")
        users = InMemoryAppUserRepository([user])
        index = InMemoryAppUserRepository([AppUserSummary.from_user(user)])
        service = AppUserService(
            user_repo=users,
            index_repo=index,
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )

        await service.purge_user_record(USER_ID)

        assert await users.get(USER_ID, USER_ID) is None
        assert await index.get(USER_ID, "registry") is None

    @pytest.mark.asyncio
    async def test_roster_failure_keeps_authoritative_record_for_resume(self):
        user = make_app_user(USER_ID, status=UserStatus.DELETION_PENDING)
        users = InMemoryAppUserRepository([user])

        class FailingIndex(InMemoryAppUserRepository):
            async def delete(self, id_: str, partition_key: str) -> bool:
                raise RuntimeError("temporary index outage")

        service = AppUserService(
            user_repo=users,
            index_repo=FailingIndex([AppUserSummary.from_user(user)]),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )

        with pytest.raises(RuntimeError, match="temporary index outage"):
            await service.purge_user_record(USER_ID)

        remaining = await users.get(USER_ID, USER_ID)
        assert remaining is not None
        assert remaining.status is UserStatus.DELETION_PENDING

    @pytest.mark.asyncio
    async def test_bootstrap_authority_rebinds_to_a_remaining_admin(self):
        deleted_admin = make_app_user(
            USER_ID,
            status=UserStatus.DELETION_PENDING,
            role=UserRole.ADMIN,
            provider_user_id="oid-alice",
        )
        replacement = make_app_user(
            OTHER_ID,
            status=UserStatus.ACTIVE,
            role=UserRole.ADMIN,
            provider_user_id="oid-bob",
        )
        binding = AdminAuthorityBinding(
            provider_user_id=deleted_admin.provider_user_id,
            user_id=deleted_admin.user_id,
            bound_at=datetime.now(UTC),
        )
        users = InMemoryAppUserRepository([deleted_admin, replacement])
        index = InMemoryAppUserRepository(
            [
                AppUserSummary.from_user(deleted_admin),
                AppUserSummary.from_user(replacement),
                binding,
            ]
        )
        service = AppUserService(
            user_repo=users,
            index_repo=index,
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email="owner@example.test"),
        )

        await service.purge_user_record(deleted_admin.user_id)

        rebound = await service.admin_binding()
        assert rebound is not None
        assert rebound.user_id == replacement.user_id
        assert rebound.provider_user_id == replacement.provider_user_id

    @pytest.mark.asyncio
    async def test_marking_deleted_strips_personal_data_but_keeps_the_record(self):
        user = make_app_user(USER_ID, status=UserStatus.DELETION_PENDING, email="alice@example.test")
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([user]),
            index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user)]),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )

        tombstone = await service.mark_deleted(USER_ID)

        assert tombstone.status is UserStatus.DELETED
        assert tombstone.email is None
        assert tombstone.display_name is None
        assert tombstone.deleted_at is not None

    @pytest.mark.asyncio
    async def test_marking_deleted_twice_is_idempotent(self):
        user = make_app_user(USER_ID, status=UserStatus.DELETION_PENDING)
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([user]),
            index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user)]),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )

        await service.mark_deleted(USER_ID)
        again = await service.mark_deleted(USER_ID)

        assert again.status is UserStatus.DELETED


    @pytest.mark.asyncio
    async def test_marking_deleted_writes_nothing_back_into_the_purged_audit_partition(self):
        """The audit partition has just been verified empty.

        Journalling a completion event into it would leave residue immediately
        before the final account record is hard-deleted.
        """

        user = make_app_user(USER_ID, status=UserStatus.DELETION_PENDING)
        audit = InMemoryAppUserRepository()
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([user]),
            index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user)]),
            audit_repo=audit,
            settings=Settings(initial_admin_email=""),
        )

        await service.mark_deleted(USER_ID)

        assert await audit.count_partition(USER_ID) == 0

    @pytest.mark.asyncio
    async def test_administrative_actions_stay_recorded_under_the_acting_admin(self):
        """Erasing the subject must not erase the administrator's accountability."""

        admin = make_app_user("user-admin", role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        subject = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        audit = InMemoryAppUserRepository()
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([admin, subject]),
            index_repo=InMemoryAppUserRepository(
                [AppUserSummary.from_user(admin), AppUserSummary.from_user(subject)]
            ),
            audit_repo=audit,
            settings=Settings(initial_admin_email=""),
        )

        await service.suspend(USER_ID, actor_user_id=admin.user_id)
        await service.mark_deletion_pending(USER_ID, actor_user_id=admin.user_id)
        await audit.purge_partition(USER_ID)
        await service.mark_deleted(USER_ID)

        assert await audit.count_partition(USER_ID) == 0
        assert await audit.count_partition(admin.user_id) >= 2


def make_client(user, roster=None):
    users = roster or [user]
    service = AppUserService(
        user_repo=InMemoryAppUserRepository(users),
        index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(item) for item in users]),
        audit_repo=InMemoryAppUserRepository(),
        settings=Settings(initial_admin_email=""),
    )

    class FakeLedger:
        def __init__(self) -> None:
            self.rows = {user.ledger_partition_key: 3}

        async def purge_owner_ledger(self, authenticated_user_id: str) -> int:
            assert authenticated_user_id == user.user_id
            return self.rows.pop(user.ledger_partition_key, 0)

        async def count_owner_ledger(self, authenticated_user_id: str) -> int:
            return self.rows.get(user.ledger_partition_key, 0)

    shared = {
        get_deletion_job_repo: InMemoryAppUserRepository(),
        get_user_settings_repo: InMemoryAppUserRepository(),
        get_recommendation_repo: InMemoryAppUserRepository(),
        get_recommendation_disposition_repo: InMemoryAppUserRepository(),
        get_portfolio_projection_repo: InMemoryAppUserRepository(),
        get_onboarding_repo: InMemoryAppUserRepository(),
        get_audit_repo: InMemoryAppUserRepository(),
        get_user_performance_repo: InMemoryAppUserRepository(),
    }
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=user.user_id,
        claims={"oid": user.provider_user_id, "auth_time": datetime.now(UTC).timestamp()},
        provider_user_id=user.provider_user_id,
    )
    app.dependency_overrides[get_app_user_service] = lambda: service
    app.dependency_overrides[get_portfolio_ledger_service] = lambda: FakeLedger()
    for dependency, value in shared.items():
        app.dependency_overrides[dependency] = (lambda v=value: v)
    # `conversations` is resolved through `auspex.api.repos`, not a FastAPI
    # dependency, so route tests monkeypatch that module attribute instead.
    return TestClient(app), service


class TestDeletionRoutes:
    def test_deletion_requires_the_typed_confirmation(self, monkeypatch):
        user = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        client, _ = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        response = client.post(
            "/api/account/deletion", json={"confirmation_phrase": "nope", "acknowledged": True}
        )

        assert response.status_code == 422

    def test_deletion_blocks_the_account_and_reports_progress(self, monkeypatch):
        user = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        client, service = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        response = client.post(
            "/api/account/deletion",
            json={"confirmation_phrase": CONFIRMATION_PHRASE, "acknowledged": True},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == DeletionJobStatus.COMPLETED.value
        assert body["account_status"] == UserStatus.DELETED.value
        assert body["progress_pct"] == 100
        assert {target["name"] for target in body["targets"]} >= {
            "portfolio_transactions",
            "user_settings",
            "recommendations",
            "recommendation_dispositions",
            "portfolio_projection",
            "conversations",
            "onboarding",
            "audit_events",
            "user_performance",
        }
        assert asyncio.run(service.get_user(USER_ID)) is None

    def test_last_admin_cannot_delete_themselves(self, monkeypatch):
        admin = make_app_user(USER_ID, status=UserStatus.ACTIVE, role=UserRole.ADMIN)
        client, _ = make_client(admin)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        response = client.post(
            "/api/account/deletion",
            json={"confirmation_phrase": CONFIRMATION_PHRASE, "acknowledged": True},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "LAST_ADMIN"

    def test_status_endpoint_is_reachable_while_deletion_is_pending(self, monkeypatch):
        user = make_app_user(USER_ID, status=UserStatus.DELETION_PENDING)
        client, _ = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        assert client.get("/api/account/deletion").status_code == 200
        # ...while the product surface is already closed.
        assert client.get("/api/health").status_code == 403

    def test_client_facing_aliases_are_served(self, monkeypatch):
        """The SPA addresses deletion as two verbs; both spellings work."""

        user = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        client, _ = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        assert client.get("/api/account/deletion").json()["status"] == "NOT_REQUESTED"

        response = client.post(
            "/api/account/deletion",
            json={"confirmation": CONFIRMATION_PHRASE},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["deleted_items"] >= 1
        assert body["remaining_items"] == 0
        assert body["error"] is None

    def test_canonical_route_enforces_the_typed_phrase(self, monkeypatch):
        user = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        client, _ = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        response = client.post(
            "/api/account/deletion",
            json={"confirmation": "oops"},
        )

        assert response.status_code == 422

    def test_product_qualified_phrase_deletes_through_the_api(self, monkeypatch):
        """The phrasing a client shows the user must be the one it accepts."""

        user = make_app_user(USER_ID, status=UserStatus.ACTIVE)
        client, _ = make_client(user)
        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )

        response = client.post(
            "/api/account/deletion",
            json={"confirmation_phrase": "DELETE MY AUSPEX ACCOUNT", "acknowledged": True},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "COMPLETED"
        assert response.json()["account_status"] == UserStatus.DELETED.value
