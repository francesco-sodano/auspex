"""Guided onboarding: resumability, idempotency and the viability rule.

Onboarding is the only path from ``APPROVED_NEEDS_ONBOARDING`` to ``ACTIVE``,
so the two things that must hold are (a) a user cannot get stuck, and (b) a
user cannot arrive in the product with a portfolio that cannot produce a
recommendation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_app_user_service,
    get_onboarding_repo,
    get_portfolio_ledger_service,
    get_user_settings_repo,
)
from auspex.models.app_user import AppUserSummary, UserStatus
from auspex.models.common import utc_now
from auspex.models.onboarding import (
    InitialPortfolio,
    InitialPositionInput,
    OnboardingAcknowledgements,
    OnboardingPreferences,
    OnboardingState,
    OnboardingStep,
)
from auspex.models.user_settings import InvestmentHorizon, RiskProfile
from auspex.settings import Settings
from auspex.users.onboarding import OnboardingError, OnboardingService
from auspex.users.service import AppUserService

from .conftest import InMemoryAppUserRepository, make_app_user

USER_ID = "user-alice"


class RecordingLedger:
    """Stands in for :class:`PortfolioLedgerService` with ledger idempotency."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def create_transaction(self, authenticated_user_id: str, payload: dict) -> dict:
        assert authenticated_user_id == USER_ID
        key = payload["client_request_id"]
        row = self.rows.setdefault(
            key, {"transaction_id": f"txn-{len(self.rows)}", "user": authenticated_user_id, **payload}
        )
        return row


def full_acknowledgements() -> OnboardingAcknowledgements:
    return OnboardingAcknowledgements(
        directional_only_acknowledged=True,
        no_guarantee_acknowledged=True,
        not_financial_advice_acknowledged=True,
        market_loss_acknowledged=True,
        independent_decision_acknowledged=True,
    )


def make_service(ledger=None) -> tuple[OnboardingService, InMemoryAppUserRepository, InMemoryAppUserRepository]:
    onboarding_repo = InMemoryAppUserRepository()
    settings_repo = InMemoryAppUserRepository()
    return OnboardingService(onboarding_repo, settings_repo, ledger), onboarding_repo, settings_repo


class TestStepProgression:
    @pytest.mark.asyncio
    async def test_fresh_state_starts_at_preferences(self):
        service, _, _ = make_service()

        state = await service.get_state(USER_ID)

        assert state.next_step() is OnboardingStep.PREFERENCES
        assert state.is_complete is False

    @pytest.mark.asyncio
    async def test_steps_advance_in_order_and_are_resumable(self):
        service, repo, _ = make_service()

        await service.set_preferences(USER_ID, OnboardingPreferences(risk_profile=RiskProfile.CONSERVATIVE))
        assert (await service.get_state(USER_ID)).next_step() is OnboardingStep.ACKNOWLEDGEMENTS

        await service.set_acknowledgements(USER_ID, full_acknowledgements())
        assert (await service.get_state(USER_ID)).next_step() is OnboardingStep.INITIAL_PORTFOLIO

        await service.set_initial_portfolio(USER_ID, InitialPortfolio(opening_cash_chf="1000"))
        state = await service.get_state(USER_ID)
        assert state.next_step() is OnboardingStep.COMPLETE
        # Resumed from storage, not from in-process memory.
        assert repo.items[(USER_ID, USER_ID)].preferences.risk_profile is RiskProfile.CONSERVATIVE

    @pytest.mark.asyncio
    async def test_resending_a_step_replaces_rather_than_duplicates(self):
        service, _, _ = make_service()

        await service.set_preferences(USER_ID, OnboardingPreferences(cash_reserve_chf="1000"))
        await service.set_preferences(USER_ID, OnboardingPreferences(cash_reserve_chf="2500"))

        state = await service.get_state(USER_ID)
        assert state.preferences.cash_reserve_chf == "2500"

    @pytest.mark.asyncio
    async def test_partial_acknowledgements_are_refused(self):
        service, _, _ = make_service()

        with pytest.raises(OnboardingError):
            await service.set_acknowledgements(
                USER_ID, OnboardingAcknowledgements(directional_only_acknowledged=True)
            )


class TestInitialPortfolioViability:
    @pytest.mark.asyncio
    async def test_empty_portfolio_is_refused(self):
        service, _, _ = make_service()

        with pytest.raises(OnboardingError):
            await service.set_initial_portfolio(USER_ID, InitialPortfolio(opening_cash_chf="0"))

    @pytest.mark.asyncio
    async def test_zero_quantity_position_is_not_viable(self):
        service, _, _ = make_service()

        with pytest.raises(OnboardingError):
            await service.set_initial_portfolio(
                USER_ID,
                InitialPortfolio(
                    opening_cash_chf="0",
                    positions=[InitialPositionInput(ticker="NVDA", quantity="0", price="100")],
                ),
            )

    @pytest.mark.asyncio
    async def test_positive_cash_alone_is_viable(self):
        service, _, _ = make_service()

        state = await service.set_initial_portfolio(USER_ID, InitialPortfolio(opening_cash_chf="0.01"))

        assert OnboardingStep.INITIAL_PORTFOLIO in state.completed_steps()

    @pytest.mark.asyncio
    async def test_positive_position_alone_is_viable(self):
        service, _, _ = make_service()

        state = await service.set_initial_portfolio(
            USER_ID,
            InitialPortfolio(
                opening_cash_chf="0",
                positions=[InitialPositionInput(ticker="nvda", quantity="3", price="100")],
            ),
        )

        assert OnboardingStep.INITIAL_PORTFOLIO in state.completed_steps()
        assert state.initial_portfolio.positions[0].ticker == "NVDA"


class TestCompletion:
    @pytest.mark.asyncio
    async def test_completion_requires_every_step(self):
        service, _, _ = make_service()
        await service.set_preferences(USER_ID, OnboardingPreferences())

        with pytest.raises(OnboardingError):
            await service.complete(USER_ID)

    @pytest.mark.asyncio
    async def test_completion_writes_settings_and_seeds_the_ledger(self):
        ledger = RecordingLedger()
        service, _, settings_repo = make_service(ledger)
        await service.set_preferences(
            USER_ID,
            OnboardingPreferences(
                risk_profile=RiskProfile.AGGRESSIVE,
                investment_horizon=InvestmentHorizon.ONE_TO_THREE_YEARS,
                cash_reserve_chf="4000",
            ),
        )
        await service.set_acknowledgements(USER_ID, full_acknowledgements())
        await service.set_initial_portfolio(
            USER_ID,
            InitialPortfolio(
                opening_cash_chf="5000",
                positions=[InitialPositionInput(ticker="NVDA", quantity="10", price="120", currency="USD")],
            ),
        )

        state = await service.complete(USER_ID)

        assert state.is_complete is True
        stored = settings_repo.items[(USER_ID, USER_ID)]
        assert stored.risk_profile is RiskProfile.AGGRESSIVE
        assert stored.investment_horizon is InvestmentHorizon.ONE_TO_THREE_YEARS
        assert stored.cash_reserve_chf == "4000"
        assert stored.acknowledged_at is not None
        types = {row["transaction_type"] for row in ledger.rows.values()}
        assert types == {"OPENING_CASH", "OPENING_POSITION"}

    @pytest.mark.asyncio
    async def test_completion_is_idempotent_and_does_not_double_seed(self):
        ledger = RecordingLedger()
        service, _, _ = make_service(ledger)
        await service.set_preferences(USER_ID, OnboardingPreferences())
        await service.set_acknowledgements(USER_ID, full_acknowledgements())
        await service.set_initial_portfolio(USER_ID, InitialPortfolio(opening_cash_chf="1000"))

        first = await service.complete(USER_ID)
        second = await service.complete(USER_ID)

        assert first.completed_at == second.completed_at
        assert len(ledger.rows) == 1


def make_client(status: UserStatus = UserStatus.APPROVED_NEEDS_ONBOARDING):
    user = make_app_user(USER_ID, status=status)
    service = AppUserService(
        user_repo=InMemoryAppUserRepository([user]),
        index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user)]),
        audit_repo=InMemoryAppUserRepository(),
        settings=Settings(initial_admin_email=""),
    )
    ledger = RecordingLedger()
    onboarding_repo = InMemoryAppUserRepository()
    settings_repo = InMemoryAppUserRepository()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=USER_ID, claims={"oid": user.provider_user_id}, provider_user_id=user.provider_user_id
    )
    app.dependency_overrides[get_app_user_service] = lambda: service
    app.dependency_overrides[get_onboarding_repo] = lambda: onboarding_repo
    app.dependency_overrides[get_user_settings_repo] = lambda: settings_repo
    app.dependency_overrides[get_portfolio_ledger_service] = lambda: ledger
    return TestClient(app), service


class TestOnboardingRoutes:
    def test_pending_user_cannot_onboard(self):
        client, _ = make_client(UserStatus.PENDING_APPROVAL)

        assert client.get("/api/onboarding").status_code == 403

    def test_active_user_cannot_re_enter_onboarding(self):
        client, _ = make_client(UserStatus.ACTIVE)

        assert client.get("/api/onboarding").status_code == 403

    def test_full_flow_activates_the_account(self):
        client, service = make_client()

        assert client.put("/api/onboarding/preferences", json={}).status_code == 200
        assert (
            client.put(
                "/api/onboarding/acknowledgements",
                json=full_acknowledgements().model_dump(mode="json"),
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/onboarding/initial-portfolio",
                json={"opening_cash_chf": "2500", "positions": []},
            ).status_code
            == 200
        )

        response = client.post("/api/onboarding/complete")

        assert response.status_code == 200
        assert response.json()["complete"] is True

    def test_non_viable_initial_portfolio_is_rejected_by_the_api(self):
        client, _ = make_client()

        response = client.put(
            "/api/onboarding/initial-portfolio", json={"opening_cash_chf": "0", "positions": []}
        )

        assert response.status_code == 422

    def test_onboarding_never_accepts_a_user_id_from_the_body(self):
        client, _ = make_client()

        client.put("/api/onboarding/preferences", json={"user_id": "user-someone-else"})

        assert client.get("/api/onboarding").json()["user_id"] == USER_ID

    def test_initial_portfolio_accepts_the_clients_field_names(self):
        """The SPA sends `client_request_id` and
        `acquisition_date`; both are absorbed rather than rejected."""

        client, _ = make_client()

        response = client.put(
            "/api/onboarding/initial-portfolio",
            json={
                "client_request_id": "req-1",
                "opening_cash_chf": "0",
                "positions": [
                    {
                        "ticker": "NVDA",
                        "quantity": "5",
                        "price": "120",
                        "currency": "USD",
                        "fx_rate_to_base": None,
                        "acquisition_date": "2026-01-15",
                    }
                ],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["initial_portfolio"]["positions"][0]["opened_on"] == "2026-01-15"

    def test_onboarding_responses_carry_the_session_projection(self):
        client, _ = make_client()

        body = client.get("/api/onboarding").json()

        for key in ("status", "role", "onboarding_completed", "created_at", "deletion_status"):
            assert key in body


class TestAcknowledgementsCapturedAtRegistration:
    """End-to-end: acknowledgements given at sign-up survive to activation.

    The regression this guards against: the wizard collects all five before
    approval, registration drops them, and the approved user then reaches the
    portfolio step with ACKNOWLEDGEMENTS outstanding — so `complete` refuses
    to activate them.
    """

    def make_approved_client(self, onboarding_repo):
        """A client for a user who has already been approved."""

        user = make_app_user(USER_ID, status=UserStatus.APPROVED_NEEDS_ONBOARDING)
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([user]),
            index_repo=InMemoryAppUserRepository([AppUserSummary.from_user(user)]),
            audit_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )
        ledger = RecordingLedger()
        settings_repo = InMemoryAppUserRepository()
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            user_id=USER_ID,
            claims={"oid": user.provider_user_id},
            provider_user_id=user.provider_user_id,
        )
        app.dependency_overrides[get_app_user_service] = lambda: service
        app.dependency_overrides[get_onboarding_repo] = lambda: onboarding_repo
        app.dependency_overrides[get_user_settings_repo] = lambda: settings_repo
        app.dependency_overrides[get_portfolio_ledger_service] = lambda: ledger
        return TestClient(app), service, settings_repo

    def seed_registration_acknowledgements(self, repo) -> None:
        """What `POST /api/session/register` now stores for this user."""

        now = utc_now()
        state = OnboardingState(
            id=USER_ID,
            user_id=USER_ID,
            started_at=now,
            updated_at=now,
            preferences=OnboardingPreferences(),
            acknowledgements=full_acknowledgements(),
        )
        state.current_step = state.next_step()
        repo.items[(USER_ID, USER_ID)] = state

    def test_approved_user_resumes_at_the_portfolio_step(self):
        repo = InMemoryAppUserRepository()
        self.seed_registration_acknowledgements(repo)
        client, _service, _settings = self.make_approved_client(repo)

        body = client.get("/api/onboarding").json()

        assert body["next_step"] == OnboardingStep.INITIAL_PORTFOLIO.value
        assert OnboardingStep.ACKNOWLEDGEMENTS.value in body["completed_steps"]

    def test_completion_succeeds_without_re_acknowledging(self):
        repo = InMemoryAppUserRepository()
        self.seed_registration_acknowledgements(repo)
        client, service, settings_repo = self.make_approved_client(repo)

        assert (
            client.put(
                "/api/onboarding/initial-portfolio",
                json={"opening_cash_chf": "2500", "positions": []},
            ).status_code
            == 200
        )
        response = client.post("/api/onboarding/complete")

        assert response.status_code == 200
        assert response.json()["complete"] is True
        # The account is genuinely active and its settings carry the
        # acknowledgement given back at sign-up.
        stored = settings_repo.items[(USER_ID, USER_ID)]
        assert stored.no_guarantee_acknowledged is True
        assert stored.acknowledged_at is not None
