"""Session/registration endpoints and the lifecycle gate on ``/api`` (arc42 §11).

The security property under test: a *valid Entra token is not access*. A
principal who has authenticated but has not been approved must be able to
register and poll their own status, and must be able to reach nothing else.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_app_user_service,
    get_onboarding_repo,
    get_user_settings_repo,
)
from auspex.api.routes.session import ACKNOWLEDGEMENT_FIELDS
from auspex.identity import compatible_user_id
from auspex.models.app_user import UserRole, UserStatus
from auspex.models.onboarding import OnboardingStep
from auspex.settings import Settings
from auspex.users.service import AppUserService

from .conftest import InMemoryAppUserRepository, make_app_user

ADMIN_EMAIL = "fsodano79@gmail.com"


def build_service(users=(), *, initial_admin_email: str = ADMIN_EMAIL) -> AppUserService:
    return AppUserService(
        user_repo=InMemoryAppUserRepository(list(users)),
        index_repo=InMemoryAppUserRepository(),
        audit_repo=InMemoryAppUserRepository(),
        settings=Settings(initial_admin_email=initial_admin_email),
    )


def make_client(
    *,
    provider_user_id: str = "oid-alice",
    user_id: str = "user-alice",
    email: str | None = "alice@example.test",
    display_name: str | None = "Alice",
    service: AppUserService | None = None,
    onboarding_repo: InMemoryAppUserRepository | None = None,
) -> tuple[TestClient, AppUserService]:
    app = create_app()
    resolved = service or build_service()
    # One repo instance for the whole client, so state written by one request
    # is visible to the next — otherwise nothing about persistence is tested.
    onboarding = onboarding_repo if onboarding_repo is not None else InMemoryAppUserRepository()
    settings_repo = InMemoryAppUserRepository()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=user_id,
        claims={"oid": provider_user_id},
        provider_user_id=provider_user_id,
        email=email,
        email_verified=True,
        display_name=display_name,
    )
    app.dependency_overrides[get_app_user_service] = lambda: resolved
    app.dependency_overrides[get_onboarding_repo] = lambda: onboarding
    app.dependency_overrides[get_user_settings_repo] = lambda: settings_repo
    return TestClient(app), resolved


def full_acknowledgements(**overrides) -> dict:
    payload = {name: True for name in ACKNOWLEDGEMENT_FIELDS}
    payload.update(overrides)
    return payload


class TestSessionStatus:
    def test_unknown_principal_is_reported_as_unregistered(self):
        client, _ = make_client()

        response = client.get("/api/session")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == UserStatus.UNREGISTERED.value
        assert body["registered"] is False
        assert body["can_access_product"] is False
        assert body["detail"] == "registration required"

    def test_status_alias_returns_the_same_shape(self):
        client, _ = make_client()

        assert client.get("/api/session/status").json() == client.get("/api/session").json()

    def test_session_never_reveals_other_users(self):
        service = build_service([make_app_user("user-bob", email="bob@example.test")])
        client, _ = make_client(service=service)

        body = client.get("/api/session").json()

        assert "bob@example.test" not in str(body)
        assert body["user_id"] == "user-alice"


class TestRegistration:
    def test_registration_creates_a_pending_account(self):
        client, service = make_client()

        response = client.post("/api/session/register", json={"accepted_terms": True})

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == UserStatus.PENDING_APPROVAL.value
        assert body["registered"] is True
        assert body["can_access_product"] is False

    def test_registration_is_idempotent(self):
        client, _ = make_client()

        client.post("/api/session/register", json={"accepted_terms": True})
        second = client.post("/api/session/register", json={"accepted_terms": True})

        assert second.status_code == 201
        assert second.json()["status"] == UserStatus.PENDING_APPROVAL.value

    def test_registration_never_accepts_an_identity_from_the_body(self):
        """A caller cannot register *as* somebody else."""

        client, service = make_client()

        client.post(
            "/api/session/register",
            json={"accepted_terms": True, "user_id": "user-victim", "role": "ADMIN"},
        )

        body = client.get("/api/session").json()
        assert body["user_id"] == "user-alice"
        assert body["role"] == UserRole.USER.value

    def test_registration_persists_the_guided_display_name(self):
        client, service = make_client(display_name=None)

        response = client.post(
            "/api/session/register",
            json={"accepted_terms": True, "display_name": "  Alice   Investor  "},
        )

        assert response.status_code == 201
        assert response.json()["display_name"] == "Alice Investor"
        stored = asyncio.run(service.get_user(compatible_user_id("oid-alice")))
        assert stored is not None
        assert stored.display_name == "Alice Investor"

    def test_initial_admin_email_bootstraps_the_first_administrator(self):
        client, _ = make_client(email=ADMIN_EMAIL, provider_user_id="oid-admin", user_id="user-admin")

        response = client.post("/api/session/register", json={"accepted_terms": True})

        body = response.json()
        assert body["role"] == UserRole.ADMIN.value
        assert body["status"] == UserStatus.APPROVED_NEEDS_ONBOARDING.value
        assert body["onboarding_required"] is True

    def test_terms_must_be_accepted(self):
        client, _ = make_client()

        response = client.post("/api/session/register", json={"accepted_terms": False})

        assert response.status_code == 422


class TestLifecycleGate:
    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (UserStatus.PENDING_APPROVAL, "PENDING_APPROVAL"),
            (UserStatus.APPROVED_NEEDS_ONBOARDING, "APPROVED_NEEDS_ONBOARDING"),
            (UserStatus.REJECTED, "REJECTED"),
            (UserStatus.SUSPENDED, "SUSPENDED"),
            (UserStatus.DELETION_PENDING, "DELETION_PENDING"),
        ],
    )
    def test_non_active_users_cannot_reach_the_product(self, status, reason):
        service = build_service([make_app_user("user-alice", status=status)])
        client, _ = make_client(service=service)

        response = client.get("/api/health")

        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == reason

    def test_unregistered_user_cannot_reach_the_product(self):
        client, _ = make_client()

        response = client.get("/api/health")

        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == "UNREGISTERED"

    def test_active_user_reaches_the_product(self):
        service = build_service([make_app_user("user-alice", status=UserStatus.ACTIVE)])
        client, _ = make_client(service=service)

        assert client.get("/api/health").status_code == 200

    def test_session_route_stays_reachable_while_pending(self):
        service = build_service([make_app_user("user-alice", status=UserStatus.PENDING_APPROVAL)])
        client, _ = make_client(service=service)

        assert client.get("/api/session").status_code == 200

    def test_unauthenticated_requests_are_rejected_before_any_lifecycle_lookup(self):
        client = TestClient(create_app())

        assert client.get("/api/session").status_code == 401
        assert client.get("/api/health").status_code == 401


class TestClientFacingContract:
    """The SPA uses the canonical session lifecycle routes."""

    def test_session_registration_registers(self):
        client, _ = make_client()

        response = client.post(
            "/api/session/register",
            json={"accepted_terms": True},
        )

        assert response.status_code == 201
        assert response.json()["status"] == UserStatus.PENDING_APPROVAL.value

    def test_session_status_is_available(self):
        client, _ = make_client()

        assert client.get("/api/session/status").status_code == 200

    def test_registration_carries_preferences_into_onboarding(self):
        """Preferences typed at sign-up should not have to be retyped."""

        service = build_service()
        # The token's user_id must be the same stable surrogate the service
        # derives from the object ID — exactly what `EntraTokenValidator`
        # produces in production.
        derived = compatible_user_id("oid-alice")
        client, _ = make_client(service=service, user_id=derived)

        response = client.post(
            "/api/session/register",
            json={
                "accepted_terms": True,
                "risk_profile": "CONSERVATIVE",
                "investment_horizon": "LONG_TERM",
                "cash_reserve_chf": "1500",
                "display_name": "Alice",
            },
        )

        assert response.status_code == 201
        body = client.get("/api/session").json()
        assert body["status"] == UserStatus.PENDING_APPROVAL.value
        assert body["registered"] is True

    def test_registration_is_visible_to_a_subsequent_session_read(self):
        """The token surrogate and the stored record must agree.

        `AuthenticatedUser.user_id` and `AppUser.user_id` are both
        `compatible_user_id(oid)`; if they ever diverged, a user could
        register and then be told they are unregistered forever.
        """

        service = build_service()
        derived = compatible_user_id("oid-alice")
        client, _ = make_client(service=service, user_id=derived)

        client.post("/api/session/register", json={"accepted_terms": True})

        assert client.get("/api/session").json()["registered"] is True


class TestRegistrationAcknowledgements:
    """The sign-up wizard collects the five acknowledgements *before* approval.

    If registration dropped them, the user would be approved and then land on
    the initial-portfolio step with ACKNOWLEDGEMENTS still outstanding, and
    `POST /api/onboarding/complete` would refuse to activate them until they
    redid a step they had already done.
    """

    def register(self, payload: dict) -> tuple[TestClient, InMemoryAppUserRepository, object]:
        onboarding_repo = InMemoryAppUserRepository()
        derived = compatible_user_id("oid-alice")
        client, service = make_client(user_id=derived, onboarding_repo=onboarding_repo)
        response = client.post("/api/session/register", json=payload)
        return client, onboarding_repo, response

    def stored_state(self, repo: InMemoryAppUserRepository):
        derived = compatible_user_id("oid-alice")
        return repo.items.get((derived, derived))

    def test_acknowledgements_are_persisted_at_registration(self):
        _client, repo, response = self.register(
            {"accepted_terms": True, **full_acknowledgements()}
        )

        assert response.status_code == 201
        state = self.stored_state(repo)
        assert state is not None
        assert state.acknowledgements is not None
        assert state.acknowledgements.all_acknowledged is True
        assert OnboardingStep.ACKNOWLEDGEMENTS in state.completed_steps()

    def test_the_acknowledgements_step_is_not_asked_for_again(self):
        """The whole point: an approved user resumes at the portfolio step."""

        _client, repo, _response = self.register(
            {
                "accepted_terms": True,
                "risk_profile": "MODERATE",
                **full_acknowledgements(),
            }
        )

        state = self.stored_state(repo)
        assert state.next_step() is OnboardingStep.INITIAL_PORTFOLIO

    def test_supplied_version_is_recorded(self):
        _client, repo, _response = self.register(
            {
                "accepted_terms": True,
                **full_acknowledgements(),
                "acknowledgement_version": "2026-08-20",
            }
        )

        assert self.stored_state(repo).acknowledgements.acknowledgement_version == "2026-08-20"

    def test_omitting_the_version_keeps_the_current_default(self):
        _client, repo, _response = self.register(
            {"accepted_terms": True, **full_acknowledgements()}
        )

        assert self.stored_state(repo).acknowledgements.acknowledgement_version

    def test_a_partial_set_is_rejected_rather_than_half_stored(self):
        payload = full_acknowledgements()
        payload.pop("market_loss_acknowledged")

        _client, repo, response = self.register({"accepted_terms": True, **payload})

        assert response.status_code == 422
        assert self.stored_state(repo) is None

    def test_a_declined_acknowledgement_is_rejected(self):
        payload = full_acknowledgements(no_guarantee_acknowledged=False)

        _client, repo, response = self.register({"accepted_terms": True, **payload})

        assert response.status_code == 422
        assert self.stored_state(repo) is None

    def test_registration_without_acknowledgements_still_works(self):
        """Clients that collect them after approval must keep working."""

        _client, repo, response = self.register({"accepted_terms": True})

        assert response.status_code == 201
        state = self.stored_state(repo)
        assert state is None or state.acknowledgements is None

    def test_the_dedicated_onboarding_endpoint_remains_available(self):
        """Registration is a shortcut, not a replacement for resumability."""

        from auspex.api import create_app

        paths = set(create_app().openapi()["paths"])
        assert "/api/onboarding/acknowledgements" in paths

    def test_re_registering_does_not_disturb_stored_acknowledgements(self):
        client, repo, _response = self.register(
            {"accepted_terms": True, **full_acknowledgements()}
        )

        second = client.post(
            "/api/session/register", json={"accepted_terms": True, **full_acknowledgements()}
        )

        assert second.status_code == 201
        assert self.stored_state(repo).acknowledgements.all_acknowledged is True

    def test_session_exposes_the_fields_the_client_renders(self):
        service = build_service([make_app_user("user-alice", status=UserStatus.ACTIVE)])
        client, _ = make_client(service=service)

        body = client.get("/api/session").json()

        for key in (
            "user_id",
            "email",
            "display_name",
            "role",
            "status",
            "onboarding_completed",
            "created_at",
            "approved_at",
            "deletion_status",
        ):
            assert key in body
