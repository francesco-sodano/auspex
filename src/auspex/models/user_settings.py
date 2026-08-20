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
    """Five non-overlapping horizon bands.

    The bands partition the timeline exactly once, with no gap and no
    overlap: ``(0, 6m]``, ``(6m, 1y]``, ``(1y, 3y]``, ``(3y, 7y]``, ``(7y, ∞)``.
    """

    SIX_MONTHS = "SIX_MONTHS"
    ONE_YEAR = "ONE_YEAR"
    ONE_TO_THREE_YEARS = "ONE_TO_THREE_YEARS"
    THREE_TO_SEVEN_YEARS = "THREE_TO_SEVEN_YEARS"
    OVER_SEVEN_YEARS = "OVER_SEVEN_YEARS"


#: Upper bound of each band in months (``None`` = unbounded). Kept beside the
#: enum so callers never have to re-derive the ordering from the member names.
HORIZON_UPPER_BOUND_MONTHS: dict[InvestmentHorizon, int | None] = {
    InvestmentHorizon.SIX_MONTHS: 6,
    InvestmentHorizon.ONE_YEAR: 12,
    InvestmentHorizon.ONE_TO_THREE_YEARS: 36,
    InvestmentHorizon.THREE_TO_SEVEN_YEARS: 84,
    InvestmentHorizon.OVER_SEVEN_YEARS: None,
}

#: Backward compatibility for documents written before the five-band split.
#: Applied on read (see :meth:`UserSettings.migrate_investment_horizon`) so
#: no stored ``user_settings`` document has to be rewritten to stay loadable,
#: and so a value that is already valid is never remapped.
LEGACY_INVESTMENT_HORIZONS: dict[str, InvestmentHorizon] = {
    "SHORT_TERM": InvestmentHorizon.ONE_TO_THREE_YEARS,
    "MEDIUM_TERM": InvestmentHorizon.THREE_TO_SEVEN_YEARS,
    "LONG_TERM": InvestmentHorizon.OVER_SEVEN_YEARS,
}


def migrate_investment_horizon(value: object) -> object:
    """Map a legacy horizon token onto its five-band replacement.

    Unknown and already-valid values pass through untouched so pydantic keeps
    producing its normal validation error for genuine garbage.
    """

    if isinstance(value, InvestmentHorizon):
        return value
    if isinstance(value, str):
        return LEGACY_INVESTMENT_HORIZONS.get(value.strip().upper(), value)
    return value


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
    investment_horizon: InvestmentHorizon = InvestmentHorizon.OVER_SEVEN_YEARS
    investment_objective: InvestmentObjective = InvestmentObjective.CAPITAL_GROWTH
    directional_only_acknowledged: bool = False
    no_guarantee_acknowledged: bool = False
    not_financial_advice_acknowledged: bool = False
    market_loss_acknowledged: bool = False
    independent_decision_acknowledged: bool = False
    acknowledgement_version: str = "2026-08-12"
    acknowledged_at: datetime | None = None
    updated_at: datetime

    @field_validator("investment_horizon", mode="before")
    @classmethod
    def migrate_investment_horizon(cls, value: object) -> object:
        """Accept pre-split horizon tokens stored in existing documents."""

        return migrate_investment_horizon(value)

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
