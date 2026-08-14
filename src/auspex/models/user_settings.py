"""Owner-scoped product settings and versioned decision-support acknowledgements."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from auspex.models.common import AuspexModel


class RiskProfile(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class InvestmentHorizon(StrEnum):
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"


class InvestmentObjective(StrEnum):
    CAPITAL_PRESERVATION = "CAPITAL_PRESERVATION"
    INCOME = "INCOME"
    BALANCED_GROWTH = "BALANCED_GROWTH"
    CAPITAL_GROWTH = "CAPITAL_GROWTH"


class UserSettings(AuspexModel):
    id: str = Field(description="user_id")
    user_id: str
    risk_profile: RiskProfile = RiskProfile.MODERATE
    cash_reserve_chf: str = "3000"
    investment_horizon: InvestmentHorizon = InvestmentHorizon.LONG_TERM
    investment_objective: InvestmentObjective = InvestmentObjective.CAPITAL_GROWTH
    directional_only_acknowledged: bool = False
    no_guarantee_acknowledged: bool = False
    not_financial_advice_acknowledged: bool = False
    market_loss_acknowledged: bool = False
    independent_decision_acknowledged: bool = False
    acknowledgement_version: str = "2026-08-12"
    acknowledged_at: datetime | None = None
    updated_at: datetime

    @field_validator("cash_reserve_chf")
    @classmethod
    def validate_cash_reserve(cls, value: str) -> str:
        from decimal import Decimal, InvalidOperation

        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("cash_reserve_chf must be a decimal number") from exc
        if parsed < 0 or parsed > 50000:
            raise ValueError("cash_reserve_chf must be between CHF 0 and CHF 50,000")
        if parsed.as_tuple().exponent < -2:
            raise ValueError("cash_reserve_chf supports at most two decimal places")
        return str(parsed)

    @property
    def partition_key(self) -> str:
        return self.user_id
