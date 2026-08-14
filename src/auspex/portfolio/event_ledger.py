"""Derives a `PortfolioSnapshot` from an immutable event ledger (arc42 §5.7).

The source of truth is **event-sourced**: `portfolio_transactions`
holds append-only transaction events (`OPENING_POSITION`, `OPENING_CASH`,
`DEPOSIT`, `WITHDRAWAL`, `DIVIDEND`, `INTEREST`, `FEE`, `BUY`, `SELL`),
partitioned by `owner_user_sk`. Auspex derives current holdings and cash by
replaying these events through pure read/derive functions.

Nothing in this module ever constructs a Cosmos write call; it only ever
transforms already-fetched event documents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from auspex.currency.money import to_decimal
from auspex.portfolio.port import Holding

# Mirrors api/auspex_api/portfolio.py's _SECURITY_TYPES / _CASH_IN_TYPES / _CASH_OUT_TYPES.
SECURITY_TRANSACTION_TYPES = frozenset({"OPENING_POSITION", "BUY", "SELL"})
_OPENING_OR_BUY_TYPES = frozenset({"OPENING_POSITION", "BUY"})


@dataclass(frozen=True)
class LedgerCostComponent:
    category: str
    amount: Decimal
    currency: str
    fx_rate_to_settlement: Decimal | None = None


@dataclass(frozen=True)
class LedgerTransaction:
    """Read-only projection of one `portfolio_transactions` event document.

    Field names match the source schema verbatim (see
    `api/auspex_api/portfolio.py::PortfolioTransaction`) so the same
    documents can be parsed without a translation table.
    """

    transaction_id: str
    owner_user_sk: str
    transaction_type: str
    event_date: date
    currency: str
    security_code: str | None
    quantity: Decimal | None
    price: Decimal | None
    cash_amount: Decimal
    fees: Decimal
    created_at: str
    cash_currency: str = "CHF"
    corrects_transaction_id: str | None = None
    linked_transaction_id: str | None = None
    affects_cash: bool = True
    client_request_id: str | None = None
    gross_amount: Decimal | None = None
    fx_rate_to_base: Decimal | None = None
    cost_category: str | None = None
    notes: str | None = None
    cost_components: tuple[LedgerCostComponent, ...] = ()
    cost_components_affect_cash: bool = False
    cost_components_explicit: bool = False
    followed_auspex: bool = False
    recommendation_id: str | None = None

    @classmethod
    def from_document(cls, document: dict) -> LedgerTransaction:
        def dec(name: str, default: str | None = None) -> Decimal | None:
            value = document.get(name, default)
            return to_decimal(value) if value is not None else None

        components = tuple(
            LedgerCostComponent(
                category=str(component.get("category", "OTHER_FEE")).upper(),
                amount=to_decimal(component.get("amount", "0")),
                currency=str(component.get("currency") or document["currency"]).upper(),
                fx_rate_to_settlement=(
                    to_decimal(component["fx_rate_to_settlement"])
                    if component.get("fx_rate_to_settlement") is not None
                    else None
                ),
            )
            for component in document.get("cost_components", [])
        )
        return cls(
            transaction_id=document["transaction_id"],
            owner_user_sk=document["owner_user_sk"],
            transaction_type=document["transaction_type"],
            event_date=date.fromisoformat(document["event_date"]),
            currency=document["currency"],
            security_code=document.get("security_code"),
            quantity=dec("quantity"),
            price=dec("price"),
            cash_amount=dec("cash_amount", "0"),
            cash_currency=str(
                document.get("cash_currency")
                or document.get("currency")
                or "CHF"
            ).upper(),
            fees=dec("fees", "0"),
            created_at=document.get("created_at", ""),
            corrects_transaction_id=document.get("corrects_transaction_id"),
            linked_transaction_id=document.get("linked_transaction_id"),
            affects_cash=document.get("affects_cash", True),
            client_request_id=document.get("client_request_id"),
            gross_amount=dec("gross_amount"),
            fx_rate_to_base=dec("fx_rate_to_base"),
            cost_category=document.get("cost_category") or document.get("category"),
            notes=document.get("notes"),
            cost_components=components,
            cost_components_affect_cash=bool(document.get("cost_components_affect_cash", False)),
            cost_components_explicit=bool(document.get("cost_components_explicit", False)),
            followed_auspex=bool(document.get("followed_auspex", False)),
            recommendation_id=document.get("recommendation_id"),
        )


def effective_transactions(transactions: list[LedgerTransaction]) -> list[LedgerTransaction]:
    """Drop transactions superseded by a later correction.

    A correction's target, and any of its linked-cost children, are excluded
    once corrected. Unlike the write-side implementation this tolerates a
    dangling or duplicate correction rather than raising — this is read-only
    display derivation, not the system of record, so a data anomaly should
    degrade the read rather than crash it.
    """

    by_id = {t.transaction_id: t for t in transactions}
    corrected: set[str] = set()
    for t in sorted(transactions, key=lambda row: (row.created_at, row.transaction_id)):
        target_id = t.corrects_transaction_id
        if target_id is None or target_id not in by_id or target_id in corrected:
            continue
        corrected.add(target_id)
    children_by_parent: dict[str, list[LedgerTransaction]] = {}
    for transaction in transactions:
        if transaction.linked_transaction_id:
            children_by_parent.setdefault(transaction.linked_transaction_id, []).append(transaction)

    effective: list[LedgerTransaction] = []
    inherited_child_parent_ids: set[str] = set()
    for transaction in transactions:
        if transaction.transaction_id in corrected or transaction.linked_transaction_id:
            continue
        if transaction.transaction_type == "VOID":
            effective.append(transaction)
            continue
        current = transaction
        if current.cost_components or current.cost_components_explicit:
            effective.append(current)
            continue
        if children_by_parent.get(current.transaction_id):
            inherited_child_parent_ids.add(current.transaction_id)
            effective.append(current)
            continue

        ancestor_id = current.corrects_transaction_id
        visited: set[str] = set()
        while ancestor_id and ancestor_id in by_id and ancestor_id not in visited:
            visited.add(ancestor_id)
            ancestor = by_id[ancestor_id]
            if ancestor.cost_components:
                current = replace(
                    current,
                    cost_components=ancestor.cost_components,
                    cost_components_affect_cash=ancestor.cost_components_affect_cash,
                )
                break
            if children_by_parent.get(ancestor_id):
                inherited_child_parent_ids.add(ancestor_id)
                break
            if ancestor.cost_components_explicit:
                break
            ancestor_id = ancestor.corrects_transaction_id
        effective.append(current)

    effective.extend(
        transaction
        for transaction in transactions
        if transaction.linked_transaction_id in inherited_child_parent_ids
        and transaction.transaction_id not in corrected
    )
    return effective


@dataclass
class _OpenLot:
    transaction_id: str
    open_date: date
    quantity: Decimal
    price: Decimal
    currency: str
    fx_rate_to_base: Decimal | None


def derive_holdings(transactions: list[LedgerTransaction]) -> list[Holding]:
    """FIFO-replay BUY/OPENING_POSITION/SELL events into current per-lot holdings.

    Read-only lot matching over the fields the port contract needs (ticker,
    quantity, cost basis, open date and lot id). Cost basis is only populated
    in USD or CHF, matching
    `Holding.cost_basis_usd`/`cost_basis_chf`; a lot opened in another
    currency (EUR/GBP) degrades those two fields to unavailable rather than
    silently misreporting them in the wrong currency.
    """

    ordered = sorted(
        (t for t in transactions if t.transaction_type in SECURITY_TRANSACTION_TYPES),
        key=lambda t: (t.event_date, t.transaction_type == "SELL", t.created_at, t.transaction_id),
    )
    lots_by_ticker: dict[str, list[_OpenLot]] = {}
    for t in ordered:
        ticker = (t.security_code or "").upper()
        if not ticker:
            continue
        lots = lots_by_ticker.setdefault(ticker, [])
        if t.transaction_type in _OPENING_OR_BUY_TYPES:
            lots.append(
                _OpenLot(
                    transaction_id=t.transaction_id,
                    open_date=t.event_date,
                    quantity=t.quantity or Decimal(0),
                    price=t.price or Decimal(0),
                    currency=t.currency,
                    fx_rate_to_base=t.fx_rate_to_base,
                )
            )
        else:  # SELL — consume the oldest open lots first
            remaining = t.quantity or Decimal(0)
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(lot.quantity, remaining)
                lot.quantity -= take
                remaining -= take
                if lot.quantity <= 0:
                    lots.pop(0)
            # A sell exceeding held quantity is a read-time data inconsistency in
            # the source; surfaced as an empty book for that ticker rather than
            # raising, since this is read-only display derivation, not the
            # system of record for the ledger itself.

    holdings: list[Holding] = []
    for ticker, lots in lots_by_ticker.items():
        for lot in lots:
            if lot.quantity <= 0:
                continue
            cost_total = lot.quantity * lot.price
            cost_basis_usd = (
                cost_total
                if lot.currency == "USD"
                else (
                    cost_total / lot.fx_rate_to_base
                    if lot.currency == "CHF" and lot.fx_rate_to_base not in (None, Decimal(0))
                    else None
                )
            )
            cost_basis_chf = (
                cost_total
                if lot.currency == "CHF"
                else (
                    cost_total * lot.fx_rate_to_base
                    if lot.fx_rate_to_base is not None
                    else None
                )
            )
            holdings.append(
                Holding(
                    ticker=ticker,
                    quantity=lot.quantity,
                    cost_basis_usd=cost_basis_usd,
                    cost_basis_chf=cost_basis_chf,
                    open_date=lot.open_date,
                    lot_id=lot.transaction_id,
                    fx_rate_at_open=lot.fx_rate_to_base,
                )
            )
    return holdings


def derive_cash_by_currency(transactions: list[LedgerTransaction]) -> dict[str, Decimal]:
    """Sum `cash_amount` per currency across every effective transaction."""

    totals: dict[str, Decimal] = {}
    for t in transactions:
        if not t.affects_cash:
            continue
        cash_currency = t.cash_currency
        totals[cash_currency] = totals.get(cash_currency, Decimal(0)) + t.cash_amount
        if t.cost_components_affect_cash:
            for component in t.cost_components:
                settlement_amount = component.amount
                if component.currency != cash_currency:
                    rate = t.fx_rate_to_base
                    if rate is None or rate <= 0:
                        raise CashCurrencyUnresolvedError(
                            f"no transaction FX rate available to settle "
                            f"{component.currency} cost in {cash_currency}"
                        )
                    settlement_amount = (
                        component.amount * rate
                        if component.currency == "USD" and cash_currency == "CHF"
                        else component.amount / rate
                    )
                totals[cash_currency] = (
                    totals.get(cash_currency, Decimal(0)) - settlement_amount
                )
    return totals


@dataclass(frozen=True)
class LedgerFinancialSummary:
    contributed_capital_chf: Decimal
    dividends_chf: Decimal
    expenses_chf: Decimal
    withdrawals_chf: Decimal


def summarize_ledger_financials(
    transactions: list[LedgerTransaction],
    fx_rate_to_chf: Callable[[str], Decimal | None],
) -> LedgerFinancialSummary:
    """Aggregate owner-facing cash-flow metrics without double-counting fees."""

    contributed_capital = Decimal(0)
    dividends = Decimal(0)
    expenses = Decimal(0)
    withdrawals = Decimal(0)
    for transaction in transactions:
        source_rate = (
            Decimal(1)
            if transaction.currency == "CHF"
            else transaction.fx_rate_to_base or fx_rate_to_chf(transaction.currency)
        )
        cash_rate = (
            Decimal(1)
            if transaction.cash_currency == "CHF"
            else transaction.fx_rate_to_base
            or fx_rate_to_chf(transaction.cash_currency)
        )
        if source_rate is None or cash_rate is None:
            continue
        if transaction.transaction_type == "DIVIDEND":
            dividends += max(transaction.cash_amount, Decimal(0)) * cash_rate
        if transaction.transaction_type in {"OPENING_CASH", "DEPOSIT"}:
            contributed_capital += max(transaction.cash_amount, Decimal(0)) * cash_rate
        if transaction.transaction_type == "OPENING_POSITION":
            contributed_capital += (
                (transaction.quantity or Decimal(0))
                * (transaction.price or Decimal(0))
                * source_rate
            )
        if transaction.transaction_type == "WITHDRAWAL":
            withdrawal = abs(transaction.cash_amount) * cash_rate
            withdrawals += withdrawal
            contributed_capital -= withdrawal
        if transaction.transaction_type in {"FEE", "TAX"}:
            expense_amount = (
                transaction.gross_amount
                if transaction.gross_amount is not None and transaction.gross_amount > 0
                else abs(transaction.cash_amount)
            )
            if expense_amount > 0:
                expense_rate = (
                    source_rate
                    if transaction.gross_amount is not None
                    and transaction.gross_amount > 0
                    else cash_rate
                )
                expenses += expense_amount * expense_rate
            else:
                for component in transaction.cost_components:
                    component_rate = (
                        Decimal(1)
                        if component.currency == "CHF"
                        else transaction.fx_rate_to_base
                        or component.fx_rate_to_settlement
                        or fx_rate_to_chf(component.currency)
                    )
                    if component_rate is not None:
                        expenses += component.amount * component_rate
        elif transaction.cost_components:
            for component in transaction.cost_components:
                component_rate = (
                    Decimal(1)
                    if component.currency == "CHF"
                    else transaction.fx_rate_to_base
                    or component.fx_rate_to_settlement
                    or fx_rate_to_chf(component.currency)
                )
                if component_rate is not None:
                    expenses += component.amount * component_rate
        elif transaction.fees:
            expenses += transaction.fees * source_rate
    return LedgerFinancialSummary(
        contributed_capital_chf=contributed_capital,
        dividends_chf=dividends,
        expenses_chf=expenses,
        withdrawals_chf=withdrawals,
    )


class CashCurrencyUnresolvedError(ValueError):
    """A non-zero cash balance exists in a currency with no CHF conversion rate.

    `cash_chf` is a REQUIRED field (arc42 §5.7): its absence means Auspex
    cannot run cash-dependent gates, so this is a hard failure, not a
    silently-dropped balance.
    """


def derive_cash_chf(
    transactions: list[LedgerTransaction],
    fx_rate_to_chf: Callable[[str], Decimal | None] | None = None,
) -> Decimal:
    """Reduce the per-currency cash balances to a single CHF figure.

    `fx_rate_to_chf` resolves a non-CHF currency code to a CHF conversion
    rate; if omitted, only a CHF-denominated book is supported (a non-zero
    balance in any other currency raises `CashCurrencyUnresolvedError`).
    """

    totals = derive_cash_by_currency(transactions)
    resolver = fx_rate_to_chf or (lambda currency: Decimal(1) if currency == "CHF" else None)
    total_chf = Decimal(0)
    for currency, amount in totals.items():
        if amount == 0:
            continue
        if currency == "CHF":
            total_chf += amount
            continue
        rate = resolver(currency)
        if rate is None:
            raise CashCurrencyUnresolvedError(
                f"no CHF conversion rate available for currency {currency!r} (balance {amount}); "
                "cash_chf is required and cannot be estimated"
            )
        total_chf += amount * rate
    return total_chf
