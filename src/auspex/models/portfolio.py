"""Auspex's own daily portfolio projection (`portfolio_projection` container,
arc42 §5.7 "Daily projection").

This is the **only** portfolio-related container Auspex creates or writes.
The live ledger is read through :mod:`auspex.portfolio.adapter` and written
through the validated append-only ledger service.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from auspex.models.common import AuspexModel


class PositionProjectionRow(AuspexModel):
    ticker: str
    quantity: str
    weight: str | None = None
    market_value_usd: str | None = None
    market_value_chf: str | None = None
    cost_basis_usd: str | None = None
    cost_basis_chf: str | None = None
    unrealised_usd: str | None = None
    unrealised_chf: str | None = None
    fx_effect_chf: str | None = None
    holding_period_days: int | None = None
    source_ledger_read_at: datetime
    degraded_fields: list[str] = Field(default_factory=list)


class PortfolioProjection(AuspexModel):
    """`portfolio_projection` container row, partitioned by `/user_id`.

    Derived and freely rewritten by Auspex every night (step 17,
    ``PROJECT_PORTFOLIO``). The transaction ledger remains the authoritative
    event history.
    """

    id: str = Field(description="{user_id}:{as_of_date}")
    user_id: str
    as_of_date: date
    lot_level: bool
    total_value_chf: str
    invested_chf: str = "0"
    total_gain_chf: str = "0"
    cash_chf: str
    dividends_chf: str = "0"
    expenses_chf: str = "0"
    withdrawals_chf: str = "0"
    positions: list[PositionProjectionRow] = Field(default_factory=list)
    degraded_fields: list[str] = Field(default_factory=list)

    @property
    def partition_key(self) -> str:
        return self.user_id
