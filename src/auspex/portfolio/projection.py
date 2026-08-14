"""Daily portfolio projection — Auspex's derived view of the live ledger (arc42 §5.7
"Daily projection").

Step 17 of the nightly pipeline (``PROJECT_PORTFOLIO``) reads the external
ledger through :class:`~auspex.portfolio.adapter.PortfolioAdapter`, joins
today's prices and FX, and writes this projection to Auspex's own
``portfolio_projection`` container. This module itself is pure derivation;
transaction mutation lives in the audited ledger service.

The market-value/unrealised/fx-effect arithmetic here is the same Decimal
math the prior FIFO-ledger revaluation used (cost basis vs. market value at
today's price and FX rate, with the FX-attributable portion of the CHF move
isolated separately) — reused here per-position rather than per-lot, since
Auspex no longer simulates lots of its own.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from auspex.currency.money import quantize_money, to_decimal
from auspex.portfolio.port import Holding, PortfolioSnapshot

# Optional Holding fields whose absence degrades a specific projected field.
_DEGRADES_MARKET_VALUE = "market_value"
_DEGRADES_COST_BASIS = "cost_basis_chf"
_DEGRADES_UNREALISED = "unrealised_chf"
_DEGRADES_FX_EFFECT = "fx_effect_chf"
_DEGRADES_HOLDING_PERIOD = "holding_period_days"


@dataclass(frozen=True)
class PositionProjection:
    ticker: str
    quantity: Decimal
    weight: Decimal | None
    market_value_usd: Decimal | None
    market_value_chf: Decimal | None
    cost_basis_usd: Decimal | None
    cost_basis_chf: Decimal | None
    unrealised_usd: Decimal | None
    unrealised_chf: Decimal | None
    fx_effect_chf: Decimal | None
    holding_period_days: int | None
    degraded_fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioProjectionResult:
    as_of_date: date
    lot_level: bool
    total_value_chf: Decimal
    invested_chf: Decimal
    total_gain_chf: Decimal
    cash_chf: Decimal
    dividends_chf: Decimal
    expenses_chf: Decimal
    withdrawals_chf: Decimal
    positions: list[PositionProjection]
    degraded_fields: list[str]  # union of every position's degraded fields, for the binding banner


def _aggregate_by_ticker(holdings: list[Holding]) -> dict[str, list[Holding]]:
    aggregated: dict[str, list[Holding]] = {}
    for holding in holdings:
        aggregated.setdefault(holding.ticker, []).append(holding)
    return aggregated


def _project_position(
    ticker: str,
    lots: list[Holding],
    price_usd: Decimal | None,
    fx_rate_chf_per_usd: Decimal,
    as_of: date,
) -> PositionProjection:
    degraded: list[str] = []
    total_quantity = sum((lot.quantity for lot in lots), Decimal(0))

    market_value_usd: Decimal | None = None
    market_value_chf: Decimal | None = None
    if price_usd is not None:
        market_value_usd = quantize_money(total_quantity * price_usd)
        market_value_chf = quantize_money(market_value_usd * fx_rate_chf_per_usd)
    else:
        degraded.append(_DEGRADES_MARKET_VALUE)

    cost_basis_usd: Decimal | None = None
    if all(lot.cost_basis_usd is not None for lot in lots) and lots:
        cost_basis_usd = sum((to_decimal(lot.cost_basis_usd) for lot in lots), Decimal(0))

    cost_basis_chf: Decimal | None = None
    if all(lot.cost_basis_chf is not None for lot in lots) and lots:
        cost_basis_chf = sum((to_decimal(lot.cost_basis_chf) for lot in lots), Decimal(0))
    elif cost_basis_usd is not None:
        cost_basis_chf = quantize_money(cost_basis_usd * fx_rate_chf_per_usd)
        degraded.append("cost_basis_chf_current_fx")
    else:
        degraded.append(_DEGRADES_COST_BASIS)

    unrealised_usd: Decimal | None = None
    if market_value_usd is not None and cost_basis_usd is not None:
        unrealised_usd = market_value_usd - cost_basis_usd

    unrealised_chf: Decimal | None = None
    if market_value_chf is not None and cost_basis_chf is not None:
        unrealised_chf = market_value_chf - cost_basis_chf
    else:
        degraded.append(_DEGRADES_UNREALISED)

    fx_effect_chf: Decimal | None = None
    if price_usd is not None and all(
        lot.fx_rate_at_open is not None and lot.cost_basis_usd is not None for lot in lots
    ) and lots:
        # Isolates the portion of the CHF move attributable purely to the USD/CHF
        # rate shifting since each lot opened, on that lot's USD cost basis.
        fx_effect_chf = sum(
            (
                quantize_money(to_decimal(lot.cost_basis_usd) * (fx_rate_chf_per_usd - to_decimal(lot.fx_rate_at_open)))
                for lot in lots
            ),
            Decimal(0),
        )
    else:
        degraded.append(_DEGRADES_FX_EFFECT)

    holding_period_days: int | None = None
    if lots and all(lot.open_date is not None for lot in lots) and total_quantity > 0:
        weighted_days = sum(((as_of - lot.open_date).days * lot.quantity for lot in lots), Decimal(0))
        holding_period_days = int((weighted_days / total_quantity).to_integral_value(rounding="ROUND_FLOOR"))
    else:
        degraded.append(_DEGRADES_HOLDING_PERIOD)

    return PositionProjection(
        ticker=ticker,
        quantity=total_quantity,
        weight=None,  # filled once total portfolio value is known
        market_value_usd=market_value_usd,
        market_value_chf=market_value_chf,
        cost_basis_usd=cost_basis_usd,
        cost_basis_chf=cost_basis_chf,
        unrealised_usd=unrealised_usd,
        unrealised_chf=unrealised_chf,
        fx_effect_chf=fx_effect_chf,
        holding_period_days=holding_period_days,
        degraded_fields=degraded,
    )


def project_portfolio(
    snapshot: PortfolioSnapshot,
    prices_usd: dict[str, Decimal],
    fx_rate_chf_per_usd: Decimal | str,
    as_of: date,
) -> PortfolioProjectionResult:
    """Join today's prices/FX onto a ledger snapshot (arc42 §5.7).

    Every policy gate only needs quantity + cash, so a position with a
    missing price still contributes ``weight=None``/``market_value=None``
    with the gap recorded in ``degraded_fields`` — never dropped silently,
    never estimated.
    """

    rate = to_decimal(fx_rate_chf_per_usd)
    by_ticker = _aggregate_by_ticker(snapshot.holdings)

    positions = [
        _project_position(ticker, lots, prices_usd.get(ticker), rate, as_of) for ticker, lots in by_ticker.items()
    ]

    total_positions_value_chf = sum(
        (p.market_value_chf for p in positions if p.market_value_chf is not None), Decimal(0)
    )
    total_value_chf = quantize_money(total_positions_value_chf + snapshot.cash_chf)
    invested_chf = quantize_money(snapshot.contributed_capital_chf)
    total_gain_chf = quantize_money(total_value_chf - invested_chf)

    final_positions: list[PositionProjection] = []
    all_degraded: set[str] = set()
    for position in positions:
        weight = (
            (position.market_value_chf / total_value_chf)
            if (position.market_value_chf is not None and total_value_chf > 0)
            else None
        )
        final_positions.append(dataclasses.replace(position, weight=weight))
        all_degraded.update(position.degraded_fields)

    return PortfolioProjectionResult(
        as_of_date=as_of,
        lot_level=snapshot.lot_level,
        total_value_chf=total_value_chf,
        invested_chf=invested_chf,
        total_gain_chf=total_gain_chf,
        cash_chf=snapshot.cash_chf,
        dividends_chf=snapshot.dividends_chf,
        expenses_chf=snapshot.expenses_chf,
        withdrawals_chf=snapshot.withdrawals_chf,
        positions=sorted(final_positions, key=lambda p: p.ticker),
        degraded_fields=sorted(all_degraded),
    )
