"""Administrator endpoints: access control, invariants, and data minimisation.

Two properties matter most here:

1. An administrator manages *access*, never *data*. No admin endpoint exposes
   another user's settings, portfolio, recommendations or ledger.
2. The deployment can never be left without an administrator, no matter which
   endpoint is used to try.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user
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
from auspex.models.app_user import AppUserSummary, UserRole, UserStatus
from auspex.settings import Settings
from auspex.users.service import AppUserService

from .conftest import InMemoryAppUserRepository, make_app_user


class FakeLedger:
    """Ledger binding for one subject, with no Azure dependency."""

    def __init__(self, user_id: str, partition_key: str) -> None:
        self.user_id = user_id
        self.partition_key = partition_key
        self.rows = {partition_key: 2}

    async def purge_owner_ledger(self, authenticated_user_id: str) -> int:
        assert authenticated_user_id == self.user_id
        return self.rows.pop(self.partition_key, 0)

    async def count_owner_ledger(self, authenticated_user_id: str) -> int:
        return self.rows.get(self.partition_key, 0)


def build_service(users) -> AppUserService:
    """Service over in-memory containers, with the roster projection seeded.

    The index is built directly rather than by replaying registrations so a
    test can start from any arbitrary lifecycle state.
    """

    isolated = [user.model_copy(deep=True) for user in users]
    return AppUserService(
        user_repo=InMemoryAppUserRepository(isolated),
        index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user) for user in isolated]),
        audit_repo=InMemoryAppUserRepository(),
        settings=Settings(initial_admin_email=""),
    )


def make_client(caller, roster) -> tuple[TestClient, AppUserService]:
    service = build_service(roster)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=caller.user_id,
        claims={"oid": caller.provider_user_id},
        provider_user_id=caller.provider_user_id,
        email=caller.email,
    )
    app.dependency_overrides[get_app_user_service] = lambda: service
    for dependency in (
        get_deletion_job_repo,
        get_user_settings_repo,
        get_recommendation_repo,
        get_recommendation_disposition_repo,
        get_portfolio_projection_repo,
        get_onboarding_repo,
        get_audit_repo,
        get_user_performance_repo,
    ):
        repo = InMemoryAppUserRepository()
        app.dependency_overrides[dependency] = (lambda r=repo: r)
    app.dependency_overrides[get_ledger_service_builder] = lambda: FakeLedger
    return TestClient(app), service


ADMIN = make_app_user("user-admin", role=UserRole.ADMIN, email="admin@example.test")
ALICE = make_app_user("user-alice", status=UserStatus.PENDING_APPROVAL, email="alice@example.test")
BOB = make_app_user("user-bob", status=UserStatus.ACTIVE, email="bob@example.test")


class TestAccessControl:
    def test_ordinary_user_cannot_reach_the_admin_surface(self):
        client, _ = make_client(BOB, [ADMIN, BOB])

        response = client.get("/api/admin/users")

        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == "NOT_ADMIN"

    def test_suspended_admin_cannot_reach_the_admin_surface(self):
        suspended_admin = make_app_user(
            "user-admin2", role=UserRole.ADMIN, status=UserStatus.SUSPENDED
        )
        client, _ = make_client(suspended_admin, [ADMIN, suspended_admin])

        response = client.get("/api/admin/users")

        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == "SUSPENDED"

    def test_admin_can_list_the_roster(self):
        client, _ = make_client(ADMIN, [ADMIN, ALICE, BOB])

        response = client.get("/api/admin/users")

        assert response.status_code == 200
        assert {row["user_id"] for row in response.json()} == {
            ADMIN.user_id,
            ALICE.user_id,
            BOB.user_id,
        }


class TestDataMinimisation:
    def test_roster_exposes_only_access_relevant_fields(self):
        client, service = make_client(ADMIN, [ADMIN, BOB])

        row = next(row for row in client.get("/api/admin/users").json() if row["user_id"] == BOB.user_id)

        # Access-management facts only: who they are, what state their access
        # is in, and who decided it. Nothing about what they hold.
        assert set(row) == {
            "user_id",
            "email",
            "display_name",
            "status",
            "role",
            "registered_at",
            "updated_at",
            "created_at",
            "onboarding_completed",
            "approved_at",
            "approved_by",
        }

    def test_no_admin_endpoint_returns_another_users_ledger_partition(self):
        client, service = make_client(ADMIN, [ADMIN, BOB])

        payload = client.get(f"/api/admin/users/{BOB.user_id}").json()

        assert "ledger_partition_key" not in payload
        assert "provider_user_id" not in payload


class TestLifecycleActions:
    def test_approve_moves_a_pending_user_to_onboarding(self):
        client, _ = make_client(ADMIN, [ADMIN, ALICE])

        response = client.post(f"/api/admin/users/{ALICE.user_id}/approve")

        assert response.status_code == 200
        assert response.json()["status"] == UserStatus.APPROVED_NEEDS_ONBOARDING.value

    def test_reject_records_the_reason(self):
        client, service = make_client(ADMIN, [ADMIN, ALICE])

        response = client.post(
            f"/api/admin/users/{ALICE.user_id}/reject", json={"reason": "not recognised"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == UserStatus.REJECTED.value

    def test_suspend_then_reinstate_round_trips(self):
        client, _ = make_client(ADMIN, [ADMIN, BOB])

        assert client.post(f"/api/admin/users/{BOB.user_id}/suspend").json()["status"] == (
            UserStatus.SUSPENDED.value
        )
        assert client.post(f"/api/admin/users/{BOB.user_id}/reinstate").json()["status"] == (
            UserStatus.ACTIVE.value
        )

    def test_illegal_transition_is_a_conflict(self):
        client, _ = make_client(ADMIN, [ADMIN, ALICE])

        # PENDING_APPROVAL cannot be reinstated — it was never suspended.
        response = client.post(f"/api/admin/users/{ALICE.user_id}/reinstate")

        assert response.status_code == 409

    def test_unknown_user_is_a_404(self):
        client, _ = make_client(ADMIN, [ADMIN])

        assert client.post("/api/admin/users/nobody/approve").status_code == 404


class TestRoleManagement:
    def test_promote_and_demote(self):
        client, _ = make_client(ADMIN, [ADMIN, BOB])

        promoted = client.post(f"/api/admin/users/{BOB.user_id}/role", json={"role": "ADMIN"})
        assert promoted.json()["role"] == UserRole.ADMIN.value

        demoted = client.post(f"/api/admin/users/{BOB.user_id}/role", json={"role": "USER"})
        assert demoted.json()["role"] == UserRole.USER.value

    def test_last_admin_cannot_be_demoted(self):
        client, _ = make_client(ADMIN, [ADMIN, BOB])

        response = client.post(f"/api/admin/users/{ADMIN.user_id}/role", json={"role": "USER"})

        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "LAST_ADMIN"

    def test_last_admin_cannot_be_suspended_or_deleted(self):
        client, _ = make_client(ADMIN, [ADMIN, BOB])

        assert client.post(f"/api/admin/users/{ADMIN.user_id}/suspend").status_code == 409
        assert client.request("DELETE", f"/api/admin/users/{ADMIN.user_id}").status_code == 409

    def test_admin_deletion_erases_the_subject_immediately(self, monkeypatch):
        """An admin-initiated deletion must actually erase, not just block.

        A path that only flipped the status would leave every private
        partition intact until the deleted user happened to sign back in —
        which for a genuine erasure request is no erasure at all.
        """

        monkeypatch.setattr(
            "auspex.api.repos.get_conversation_repo", lambda: InMemoryAppUserRepository()
        )
        client, service = make_client(ADMIN, [ADMIN, BOB])

        response = client.request("DELETE", f"/api/admin/users/{BOB.user_id}")

        assert response.status_code == 200
        assert response.json()["status"] == UserStatus.DELETED.value
        assert response.json()["email"] is None
        assert asyncio.run(service.get_user(BOB.user_id)) is None
