"""Binds :class:`~auspex.portfolio.port.PortfolioSnapshot` against
``config/universe.yaml`` at bootstrap (arc42 §6.3 step 11).

An unmapped ticker is the dangerous failure mode: a position Auspex cannot
see is a position it cannot advise on, and the owner would otherwise have no
signal that it was invisible. This is therefore a **hard failure**, not a
degraded field.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.config.loader import Universe
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.port import PortfolioSnapshot


@dataclass(frozen=True)
class BindingValidationResult:
    snapshot: PortfolioSnapshot
    unmapped_tickers: list[str]
    sample_document: dict | None

    @property
    def is_valid(self) -> bool:
        return not self.unmapped_tickers


async def validate_portfolio_binding(
    adapter: PortfolioAdapter,
    universe: Universe,
    as_of: date,
    fx_rate_to_chf: Callable[[str], Decimal | None] | None = None,
) -> BindingValidationResult:
    """arc42 §6.3 step 11: resolve mapping, assert every ticker maps to the
    universe, determine ``lot_level``, and capture a sample document for
    owner confirmation before the run proceeds."""

    snapshot = await adapter.read_snapshot(as_of, fx_rate_to_chf=fx_rate_to_chf)
    sample = await adapter.sample_holding_document()

    universe_tickers = {s.ticker for s in universe.securities}
    unmapped = sorted({h.ticker for h in snapshot.holdings if h.ticker not in universe_tickers})

    return BindingValidationResult(snapshot=snapshot, unmapped_tickers=unmapped, sample_document=sample)
