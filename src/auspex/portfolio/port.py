"""Portfolio read port (arc42 §5.7 "The port").

`auspex/portfolio/port.py` defines exactly what Auspex needs from the
existing, owner-controlled portfolio ledger. Everything else about that
source schema is irrelevant to it — see :mod:`auspex.portfolio.mapping` for
how field names are configured and :mod:`auspex.portfolio.adapter` for the
Cosmos binding. Mutation is handled by the audited ledger write service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Holding:
    """One lot, or one aggregated position if the source ledger has no lot-level detail.

    Every policy gate depends only on ``ticker`` and ``quantity`` (arc42
    §5.7): position weight is ``quantity * price / portfolio value``, no
    cost basis needed. The optional fields only improve display and degrade
    to unavailable — never to an estimate — when absent.
    """

    ticker: str  # REQUIRED — resolved to security_id via config/universe.yaml
    quantity: Decimal  # REQUIRED
    # --- optional enrichment; absence degrades gracefully ---
    cost_basis_usd: Decimal | None = None
    cost_basis_chf: Decimal | None = None
    open_date: date | None = None
    lot_id: str | None = None
    fx_rate_at_open: Decimal | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    """The full ledger view Auspex needs for one day."""

    holdings: list[Holding]  # one entry per lot, or per position if lots absent
    cash_chf: Decimal  # REQUIRED
    as_of: date
    lot_level: bool  # true if holdings are lots, false if aggregated positions
    dividends_chf: Decimal = Decimal(0)
    expenses_chf: Decimal = Decimal(0)
    withdrawals_chf: Decimal = Decimal(0)
    contributed_capital_chf: Decimal = Decimal(0)


class PortfolioPort(Protocol):
    """What Auspex needs from a portfolio source. Nothing else about the
    source schema — or whether it even lives in Cosmos — is visible here."""

    async def read_snapshot(
        self,
        as_of: date,
        fx_rate_to_chf: Callable[[str], Decimal | None] | None = None,
    ) -> PortfolioSnapshot: ...
