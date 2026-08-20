"""Guided onboarding for an approved user (arc42 §5.7, §8.3).

Three steps, all idempotent and independently resumable:

1. **preferences** — risk profile, horizon, objective, cash reserve;
2. **acknowledgements** — the five regulatory acknowledgements;
3. **initial portfolio** — the declared starting ledger, which must be
   viable: opening CHF cash strictly above zero, or at least one stock
   position with strictly positive quantity.

Completion writes the user's :class:`~auspex.models.user_settings.UserSettings`,
seeds the declared opening balances into that user's own event ledger, and
transitions the account to ``ACTIVE``.

Every step re-sends the whole payload for that step, so a retried or
duplicated request converges on the same state instead of appending. Ledger
seeding is made replay-safe by deriving a deterministic
``client_request_id`` per seeded row and recording the resulting transaction
ids on the onboarding document: a crash between "ledger written" and
"onboarding updated" is recovered by the ledger's own
``client_request_id`` idempotency.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from auspex.models.common import utc_now
from auspex.models.onboarding import (
    InitialPortfolio,
    OnboardingAcknowledgements,
    OnboardingPreferences,
    OnboardingState,
    OnboardingStep,
)
from auspex.models.user_settings import UserSettings


class OnboardingError(ValueError):
    """Onboarding cannot proceed in the requested way."""


class _Repository(Protocol):
    async def get(self, id_: str, partition_key: str) -> Any: ...

    async def upsert(self, item: Any) -> None: ...


class _LedgerWriter(Protocol):
    async def create_transaction(
        self, authenticated_user_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...


class OnboardingService:
    def __init__(
        self,
        onboarding_repo: _Repository,
        user_settings_repo: _Repository,
        ledger: _LedgerWriter | None = None,
    ) -> None:
        self._onboarding = onboarding_repo
        self._settings_repo = user_settings_repo
        self._ledger = ledger

    async def get_state(self, user_id: str, *, now: datetime | None = None) -> OnboardingState:
        """Current onboarding state, creating an empty one on first access."""

        existing = await self._onboarding.get(user_id, user_id)
        if isinstance(existing, OnboardingState):
            return existing
        moment = now or utc_now()
        state = OnboardingState(id=user_id, user_id=user_id, started_at=moment, updated_at=moment)
        await self._onboarding.upsert(state)
        return state

    async def set_preferences(
        self, user_id: str, preferences: OnboardingPreferences, *, now: datetime | None = None
    ) -> OnboardingState:
        state = await self._require_incomplete(user_id, now=now)
        state.preferences = preferences
        return await self._save(state, now=now)

    async def set_acknowledgements(
        self, user_id: str, acknowledgements: OnboardingAcknowledgements, *, now: datetime | None = None
    ) -> OnboardingState:
        if not acknowledgements.all_acknowledged:
            raise OnboardingError("all five decision-support acknowledgements are required")
        state = await self._require_incomplete(user_id, now=now)
        state.acknowledgements = acknowledgements
        return await self._save(state, now=now)

    async def set_initial_portfolio(
        self, user_id: str, portfolio: InitialPortfolio, *, now: datetime | None = None
    ) -> OnboardingState:
        if not portfolio.is_viable():
            raise OnboardingError(
                "the initial portfolio needs opening CHF cash greater than zero "
                "or at least one position with a positive quantity"
            )
        state = await self._require_incomplete(user_id, now=now)
        state.initial_portfolio = portfolio
        return await self._save(state, now=now)

    async def complete(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> OnboardingState:
        """Finish onboarding: persist settings, seed the ledger, mark complete.

        Idempotent — calling it again on a completed onboarding returns the
        existing state without re-seeding the ledger.
        """

        moment = now or utc_now()
        state = await self.get_state(user_id, now=moment)
        if state.is_complete:
            return state
        if not state.can_complete():
            raise OnboardingError(f"onboarding is incomplete: next required step is {state.next_step().value}")

        assert state.preferences is not None  # guaranteed by can_complete()
        assert state.acknowledgements is not None
        assert state.initial_portfolio is not None

        await self._settings_repo.upsert(
            UserSettings(
                id=user_id,
                user_id=user_id,
                risk_profile=state.preferences.risk_profile,
                cash_reserve_chf=state.preferences.cash_reserve_chf,
                investment_horizon=state.preferences.investment_horizon,
                investment_objective=state.preferences.investment_objective,
                directional_only_acknowledged=True,
                no_guarantee_acknowledged=True,
                not_financial_advice_acknowledged=True,
                market_loss_acknowledged=True,
                independent_decision_acknowledged=True,
                acknowledgement_version=state.acknowledgements.acknowledgement_version,
                acknowledged_at=moment,
                updated_at=moment,
            )
        )

        seeded = await self._seed_ledger(user_id, state.initial_portfolio, moment)
        for transaction_id in seeded:
            if transaction_id not in state.seeded_transaction_ids:
                state.seeded_transaction_ids.append(transaction_id)

        state.current_step = OnboardingStep.COMPLETE
        state.completed_at = moment
        state.updated_at = moment
        await self._onboarding.upsert(state)
        return state

    async def _seed_ledger(
        self, user_id: str, portfolio: InitialPortfolio, moment: datetime
    ) -> list[str]:
        """Write opening cash / opening positions into the user's own ledger."""

        if self._ledger is None:
            return []
        opened_on = (portfolio.opened_on or moment.date()).isoformat()
        written: list[str] = []

        opening_cash = Decimal(portfolio.opening_cash_chf)
        if opening_cash > 0:
            row = await self._ledger.create_transaction(
                user_id,
                {
                    "client_request_id": f"onboarding:{user_id}:opening_cash",
                    "transaction_type": "OPENING_CASH",
                    "event_date": opened_on,
                    "amount": str(opening_cash),
                    "currency": "CHF",
                    "fx_rate_to_base": "1",
                    "fees": "0",
                    "broker_commission": "0",
                    "stamp_duty": "0",
                    "taxes": "0",
                },
            )
            written.append(str(row.get("transaction_id") or row.get("id")))

        for index, position in enumerate(portfolio.positive_positions):
            row = await self._ledger.create_transaction(
                user_id,
                {
                    "client_request_id": f"onboarding:{user_id}:position:{index}:{position.ticker}",
                    "transaction_type": "OPENING_POSITION",
                    "event_date": (position.opened_on or portfolio.opened_on or moment.date()).isoformat(),
                    "security_code": position.ticker,
                    "quantity": position.quantity,
                    "price": position.price,
                    "currency": position.currency,
                    "fx_rate_to_base": position.fx_rate_to_base or "1",
                    "fees": "0",
                    "broker_commission": "0",
                    "stamp_duty": "0",
                    "taxes": "0",
                },
            )
            written.append(str(row.get("transaction_id") or row.get("id")))
        return written

    async def _require_incomplete(self, user_id: str, *, now: datetime | None) -> OnboardingState:
        state = await self.get_state(user_id, now=now)
        if state.is_complete:
            raise OnboardingError("onboarding is already complete")
        return state

    async def _save(self, state: OnboardingState, *, now: datetime | None) -> OnboardingState:
        state.updated_at = now or utc_now()
        state.current_step = state.next_step()
        await self._onboarding.upsert(state)
        return state
