"""Guided onboarding state for an approved user (`onboarding` container).

Onboarding is resumable and idempotent: each step is a full replace of that
step's payload keyed on ``user_id``, so a client that loses connectivity can
re-send the same step without creating duplicates, and a client returning
days later resumes exactly where it stopped.

A user only becomes :class:`~auspex.models.app_user.UserStatus.ACTIVE` when
all three steps are complete and the declared initial portfolio is viable:
either opening CHF cash strictly greater than zero, or at least one stock
position with a strictly positive quantity (arc42 §5.7 — a projection with
neither cash nor holdings cannot produce a meaningful recommendation).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import AliasChoices, ConfigDict, Field, field_validator

from auspex.models.common import AuspexModel
from auspex.models.user_settings import (
    InvestmentHorizon,
    InvestmentObjective,
    RiskProfile,
)


class OnboardingStep(StrEnum):
    PREFERENCES = "PREFERENCES"
    ACKNOWLEDGEMENTS = "ACKNOWLEDGEMENTS"
    INITIAL_PORTFOLIO = "INITIAL_PORTFOLIO"
    COMPLETE = "COMPLETE"


ONBOARDING_STEP_ORDER: tuple[OnboardingStep, ...] = (
    OnboardingStep.PREFERENCES,
    OnboardingStep.ACKNOWLEDGEMENTS,
    OnboardingStep.INITIAL_PORTFOLIO,
)


def _validated_decimal(value: str, field_name: str, *, max_scale: int = 2) -> str:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal number") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must not be negative")
    exponent = parsed.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -max_scale:
        raise ValueError(f"{field_name} supports at most {max_scale} decimal places")
    return str(parsed)


class OnboardingPreferences(AuspexModel):
    risk_profile: RiskProfile = RiskProfile.MODERATE
    investment_horizon: InvestmentHorizon = InvestmentHorizon.OVER_SEVEN_YEARS
    investment_objective: InvestmentObjective = InvestmentObjective.CAPITAL_GROWTH
    cash_reserve_chf: str = "3000"

    @field_validator("cash_reserve_chf")
    @classmethod
    def _validate_cash_reserve(cls, value: str) -> str:
        validated = _validated_decimal(value, "cash_reserve_chf")
        if Decimal(validated) > 50000:
            raise ValueError("cash_reserve_chf must be between CHF 0 and CHF 50,000")
        return validated


class OnboardingAcknowledgements(AuspexModel):
    """The five regulatory acknowledgements required before any recommendation
    may be shown (arc42 §8.3). All five must be true; a partial set is not a
    valid acknowledgement."""

    directional_only_acknowledged: bool = False
    no_guarantee_acknowledged: bool = False
    not_financial_advice_acknowledged: bool = False
    market_loss_acknowledged: bool = False
    independent_decision_acknowledged: bool = False
    acknowledgement_version: str = "2026-08-12"

    @property
    def all_acknowledged(self) -> bool:
        return all(
            (
                self.directional_only_acknowledged,
                self.no_guarantee_acknowledged,
                self.not_financial_advice_acknowledged,
                self.market_loss_acknowledged,
                self.independent_decision_acknowledged,
            )
        )


class InitialPositionInput(AuspexModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str
    quantity: str
    price: str
    currency: str = "USD"
    fx_rate_to_base: str | None = None
    opened_on: date | None = Field(
        default=None,
        validation_alias=AliasChoices("opened_on", "acquisition_date"),
        description="when the lot was acquired; accepts the client's `acquisition_date` spelling",
    )

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker is required")
        return normalized

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CHF", "USD"}:
            raise ValueError("currency must be CHF or USD")
        return normalized

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, value: str) -> str:
        return _validated_decimal(value, "quantity", max_scale=6)

    @field_validator("price")
    @classmethod
    def _validate_price(cls, value: str) -> str:
        return _validated_decimal(value, "price", max_scale=6)

    @field_validator("fx_rate_to_base")
    @classmethod
    def _validate_fx(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validated = _validated_decimal(value, "fx_rate_to_base", max_scale=8)
        if Decimal(validated) <= 0:
            raise ValueError("fx_rate_to_base must be positive")
        return validated

    @property
    def is_positive(self) -> bool:
        return Decimal(self.quantity) > 0


class InitialPortfolio(AuspexModel):
    """Declared starting point of the user's ledger.

    Viability rule (enforced by :meth:`is_viable`): opening CHF cash strictly
    positive, **or** at least one position with strictly positive quantity.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    opening_cash_chf: str = "0"
    positions: list[InitialPositionInput] = Field(default_factory=list)
    opened_on: date | None = None

    @field_validator("opening_cash_chf")
    @classmethod
    def _validate_cash(cls, value: str) -> str:
        return _validated_decimal(value, "opening_cash_chf")

    @field_validator("positions")
    @classmethod
    def _bound_positions(cls, value: list[InitialPositionInput]) -> list[InitialPositionInput]:
        if len(value) > 50:
            raise ValueError("initial portfolio supports at most 50 positions")
        return value

    @property
    def positive_positions(self) -> list[InitialPositionInput]:
        return [position for position in self.positions if position.is_positive]

    def is_viable(self) -> bool:
        return Decimal(self.opening_cash_chf) > 0 or bool(self.positive_positions)


class OnboardingState(AuspexModel):
    """`onboarding` container row, partitioned by ``/user_id``."""

    id: str = Field(description="user_id")
    user_id: str
    current_step: OnboardingStep = OnboardingStep.PREFERENCES
    preferences: OnboardingPreferences | None = None
    acknowledgements: OnboardingAcknowledgements | None = None
    initial_portfolio: InitialPortfolio | None = None
    seeded_transaction_ids: list[str] = Field(
        default_factory=list,
        description="ledger transaction ids already written for the initial portfolio (replay-safe)",
    )
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @property
    def partition_key(self) -> str:
        return self.user_id

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    def completed_steps(self) -> list[OnboardingStep]:
        done: list[OnboardingStep] = []
        if self.preferences is not None:
            done.append(OnboardingStep.PREFERENCES)
        if self.acknowledgements is not None and self.acknowledgements.all_acknowledged:
            done.append(OnboardingStep.ACKNOWLEDGEMENTS)
        if self.initial_portfolio is not None and self.initial_portfolio.is_viable():
            done.append(OnboardingStep.INITIAL_PORTFOLIO)
        return done

    def next_step(self) -> OnboardingStep:
        done = set(self.completed_steps())
        for step in ONBOARDING_STEP_ORDER:
            if step not in done:
                return step
        return OnboardingStep.COMPLETE

    def can_complete(self) -> bool:
        return self.next_step() is OnboardingStep.COMPLETE
