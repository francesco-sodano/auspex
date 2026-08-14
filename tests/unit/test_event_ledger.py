"""Unit tests for deterministic event-ledger derivation (arc42 §5.7)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from auspex.portfolio.event_ledger import (
    CashCurrencyUnresolvedError,
    LedgerCostComponent,
    LedgerTransaction,
    derive_cash_by_currency,
    derive_cash_chf,
    derive_holdings,
    effective_transactions,
    summarize_ledger_financials,
)


def txn(**overrides) -> LedgerTransaction:
    base = dict(
        transaction_id="t1",
        owner_user_sk="owner-1-sk",
        transaction_type="BUY",
        event_date=date(2026, 1, 5),
        currency="USD",
        security_code="NVDA",
        quantity=Decimal(10),
        price=Decimal(150),
        cash_amount=Decimal(0),
        fees=Decimal(0),
        created_at="2026-01-05T00:00:00Z",
    )
    base.update(overrides)
    return LedgerTransaction(**base)


class TestFromDocument:
    def test_parses_document_fields(self):
        doc = {
            "transaction_id": "t1",
            "owner_user_sk": "owner-1-sk",
            "transaction_type": "BUY",
            "event_date": "2026-01-05",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "10",
            "price": "150",
            "cash_amount": "-1500",
            "fees": "0",
            "created_at": "2026-01-05T00:00:00Z",
        }
        t = LedgerTransaction.from_document(doc)
        assert t.quantity == Decimal(10)
        assert t.price == Decimal(150)
        assert t.cash_amount == Decimal("-1500")
        assert t.event_date == date(2026, 1, 5)

    def test_missing_optional_numeric_fields_default(self):
        doc = {
            "transaction_id": "c1",
            "owner_user_sk": "owner-1-sk",
            "transaction_type": "OPENING_CASH",
            "event_date": "2026-01-02",
            "currency": "CHF",
            "cash_amount": "1000",
            "created_at": "2026-01-02T00:00:00Z",
        }
        t = LedgerTransaction.from_document(doc)
        assert t.fees == Decimal(0)
        assert t.quantity is None
        assert t.security_code is None


class TestEffectiveTransactions:
    def test_uncorrected_transactions_pass_through(self):
        txns = [txn(transaction_id="t1"), txn(transaction_id="t2")]
        result = effective_transactions(txns)
        assert {t.transaction_id for t in result} == {"t1", "t2"}

    def test_corrected_transaction_excluded(self):
        original = txn(transaction_id="t1", quantity=Decimal(10))
        correction = txn(
            transaction_id="t2",
            corrects_transaction_id="t1",
            quantity=Decimal(7),
            created_at="2026-01-06T00:00:00Z",
        )
        result = effective_transactions([original, correction])
        ids = {t.transaction_id for t in result}
        assert ids == {"t2"}

    def test_dangling_correction_does_not_crash(self):
        correction = txn(transaction_id="t2", corrects_transaction_id="does-not-exist")
        result = effective_transactions([correction])
        assert len(result) == 1

    def test_duplicate_correction_does_not_crash(self):
        original = txn(transaction_id="t1")
        correction_a = txn(transaction_id="t2", corrects_transaction_id="t1", created_at="2026-01-06T00:00:00Z")
        correction_b = txn(transaction_id="t3", corrects_transaction_id="t1", created_at="2026-01-07T00:00:00Z")
        result = effective_transactions([original, correction_a, correction_b])
        ids = {t.transaction_id for t in result}
        assert "t1" not in ids


class TestDeriveHoldings:
    def test_single_buy_produces_one_holding(self):
        holdings = derive_holdings([txn(transaction_id="t1", quantity=Decimal(10), price=Decimal(150))])
        assert len(holdings) == 1
        assert holdings[0].ticker == "NVDA"
        assert holdings[0].quantity == Decimal(10)
        assert holdings[0].cost_basis_usd == Decimal(1500)
        assert holdings[0].cost_basis_chf is None

    def test_sell_consumes_oldest_lot_first_fifo(self):
        lot1 = txn(
            transaction_id="lot1",
            quantity=Decimal(10),
            price=Decimal(100),
            event_date=date(2026, 1, 1),
            created_at="2026-01-01T00:00:00Z",
        )
        lot2 = txn(
            transaction_id="lot2",
            quantity=Decimal(5),
            price=Decimal(200),
            event_date=date(2026, 1, 3),
            created_at="2026-01-03T00:00:00Z",
        )
        sell = txn(
            transaction_id="sell1",
            transaction_type="SELL",
            quantity=Decimal(12),
            event_date=date(2026, 1, 10),
            created_at="2026-01-10T00:00:00Z",
        )
        holdings = derive_holdings([lot1, lot2, sell])
        # 10 consumed entirely from lot1, 2 consumed from lot2, leaving 3 in lot2
        assert len(holdings) == 1
        assert holdings[0].lot_id == "lot2"
        assert holdings[0].quantity == Decimal(3)

    def test_fully_sold_lot_is_dropped(self):
        buy = txn(transaction_id="b1", quantity=Decimal(5))
        sell = txn(transaction_id="s1", transaction_type="SELL", quantity=Decimal(5), event_date=date(2026, 1, 10))
        holdings = derive_holdings([buy, sell])
        assert holdings == []

    def test_multiple_tickers_kept_separate(self):
        nvda = txn(transaction_id="n1", security_code="NVDA", quantity=Decimal(10))
        amd = txn(transaction_id="a1", security_code="AMD", quantity=Decimal(5))
        holdings = derive_holdings([nvda, amd])
        tickers = {h.ticker for h in holdings}
        assert tickers == {"NVDA", "AMD"}

    def test_non_security_transactions_ignored(self):
        cash = txn(transaction_id="c1", transaction_type="OPENING_CASH", security_code=None, quantity=None)
        holdings = derive_holdings([cash])
        assert holdings == []


class TestDeriveCash:
    def test_single_currency_totals(self):
        totals = derive_cash_by_currency(
            [txn(transaction_id="c1", currency="CHF", cash_amount=Decimal(1000), transaction_type="OPENING_CASH")]
        )
        assert totals == {"CHF": Decimal(1000)}

    def test_multi_currency_totals_kept_separate(self):
        totals = derive_cash_by_currency(
            [
                txn(transaction_id="c1", currency="CHF", cash_amount=Decimal(1000)),
                txn(
                    transaction_id="c2",
                    currency="USD",
                    cash_currency="USD",
                    cash_amount=Decimal(500),
                ),
            ]
        )
        assert totals == {"CHF": Decimal(1000), "USD": Decimal(500)}

    def test_derive_cash_chf_pure_chf_book(self):
        chf = derive_cash_chf([txn(transaction_id="c1", currency="CHF", cash_amount=Decimal(1000))])
        assert chf == Decimal(1000)

    def test_derive_cash_chf_raises_without_resolver_for_foreign_currency(self):
        with pytest.raises(CashCurrencyUnresolvedError):
            derive_cash_chf(
                [
                    txn(
                        transaction_id="c1",
                        currency="USD",
                        cash_currency="USD",
                        cash_amount=Decimal(500),
                    )
                ]
            )

    def test_derive_cash_chf_uses_resolver(self):
        chf = derive_cash_chf(
            [
                txn(
                    transaction_id="c1",
                    currency="USD",
                    cash_currency="USD",
                    cash_amount=Decimal(500),
                )
            ],
            fx_rate_to_chf=lambda ccy: Decimal("0.9") if ccy == "USD" else None,
        )
        assert chf == Decimal("450.0")

    def test_zero_balance_foreign_currency_does_not_require_resolver(self):
        chf = derive_cash_chf([txn(transaction_id="c1", currency="USD", cash_amount=Decimal(0))])
        assert chf == Decimal(0)

    def test_non_cash_void_event_does_not_change_cash(self):
        totals = derive_cash_by_currency(
            [txn(transaction_id="void", currency="CHF", cash_amount=Decimal("999"), affects_cash=False)]
        )
        assert totals == {}


def test_summarizes_dividends_expenses_and_withdrawals_in_chf() -> None:
    summary = summarize_ledger_financials(
        [
            txn(
                transaction_id="d",
                transaction_type="DIVIDEND",
                currency="USD",
                cash_currency="USD",
                cash_amount=Decimal("100"),
            ),
            txn(transaction_id="f", transaction_type="FEE", currency="CHF", cash_amount=Decimal("-20")),
            txn(transaction_id="w", transaction_type="WITHDRAWAL", currency="CHF", cash_amount=Decimal("-200")),
            txn(transaction_id="b", transaction_type="BUY", currency="USD", fees=Decimal("5")),
        ],
        fx_rate_to_chf=lambda currency: Decimal("0.8") if currency == "USD" else Decimal(1),
    )
    assert summary.dividends_chf == Decimal("80")
    assert summary.expenses_chf == Decimal("24")
    assert summary.withdrawals_chf == Decimal("200")
    assert summary.contributed_capital_chf == Decimal("-200")


def test_summarizes_zero_cash_opening_cost_from_recorded_gross_amount() -> None:
    summary = summarize_ledger_financials(
        [
            txn(
                transaction_id="opening-commission",
                transaction_type="FEE",
                currency="CHF",
                cash_amount=Decimal("0"),
                gross_amount=Decimal("12.50"),
                linked_transaction_id="opening-position",
                affects_cash=False,
            )
        ],
        fx_rate_to_chf=lambda _currency: Decimal(1),
    )

    assert summary.expenses_chf == Decimal("12.50")


def test_zero_cash_fee_falls_back_to_its_cost_components() -> None:
    summary = summarize_ledger_financials(
        [
            txn(
                transaction_id="fee-breakdown",
                transaction_type="FEE",
                currency="CHF",
                cash_amount=Decimal("0"),
                gross_amount=Decimal("0"),
                cost_components=(
                    LedgerCostComponent(
                        category="OTHER_FEE",
                        amount=Decimal("12.34"),
                        currency="CHF",
                    ),
                ),
            )
        ],
        fx_rate_to_chf=lambda _currency: Decimal(1),
    )

    assert summary.expenses_chf == Decimal("12.34")


def test_nested_costs_use_shared_transaction_fx_and_own_cash_currency() -> None:
    transaction = txn(
        transaction_id="buy",
        transaction_type="BUY",
        currency="USD",
        cash_amount=Decimal("-100"),
        fx_rate_to_base=Decimal("0.8"),
        cost_components=(
            LedgerCostComponent(
                category="BROKER_COMMISSION",
                amount=Decimal("5"),
                currency="CHF",
            ),
            LedgerCostComponent(
                category="TRANSACTION_TAX",
                amount=Decimal("2"),
                currency="USD",
            ),
        ),
        cost_components_affect_cash=True,
    )

    assert derive_cash_by_currency([transaction]) == {
        "CHF": Decimal("-106.6"),
    }
    summary = summarize_ledger_financials(
        [transaction],
        fx_rate_to_chf=lambda currency: Decimal("0.8") if currency == "USD" else Decimal(1),
    )
    assert summary.expenses_chf == Decimal("6.6")


def test_accidental_legacy_correction_inherits_nearest_parent_cost_children() -> None:
    original = txn(
        transaction_id="opening",
        transaction_type="OPENING_POSITION",
        currency="USD",
        quantity=Decimal("10"),
        price=Decimal("100"),
        cash_amount=Decimal(0),
    )
    child = txn(
        transaction_id="commission",
        transaction_type="FEE",
        currency="CHF",
        quantity=None,
        price=None,
        cash_amount=Decimal(0),
        gross_amount=Decimal("12"),
        linked_transaction_id="opening",
        affects_cash=False,
    )
    correction = txn(
        transaction_id="opening-correction",
        transaction_type="OPENING_POSITION",
        currency="USD",
        quantity=Decimal("11"),
        price=Decimal("100"),
        cash_amount=Decimal(0),
        corrects_transaction_id="opening",
        created_at="2026-02-01T00:00:00Z",
    )

    effective = effective_transactions([original, child, correction])

    assert {row.transaction_id for row in effective} == {
        "commission",
        "opening-correction",
    }
    summary = summarize_ledger_financials(
        effective,
        fx_rate_to_chf=lambda currency: Decimal("0.8") if currency == "USD" else Decimal(1),
    )
    assert summary.expenses_chf == Decimal("12")


def test_explicit_cost_removal_does_not_inherit_corrected_parent_children() -> None:
    original = txn(
        transaction_id="opening",
        transaction_type="OPENING_POSITION",
        cash_amount=Decimal(0),
    )
    child = txn(
        transaction_id="commission",
        transaction_type="FEE",
        currency="CHF",
        cash_amount=Decimal(0),
        gross_amount=Decimal("12"),
        linked_transaction_id="opening",
        affects_cash=False,
    )
    correction = txn(
        transaction_id="opening-correction",
        transaction_type="OPENING_POSITION",
        cash_amount=Decimal(0),
        corrects_transaction_id="opening",
        cost_components_explicit=True,
        created_at="2026-02-01T00:00:00Z",
    )

    effective = effective_transactions([original, child, correction])

    assert [row.transaction_id for row in effective] == ["opening-correction"]


def test_external_contributions_exclude_buys_but_include_opening_positions() -> None:
    summary = summarize_ledger_financials(
        [
            txn(transaction_id="cash", transaction_type="OPENING_CASH", currency="CHF", cash_amount=Decimal("1000")),
            txn(
                transaction_id="opening",
                transaction_type="OPENING_POSITION",
                currency="USD",
                quantity=Decimal("10"),
                price=Decimal("50"),
                cash_amount=Decimal("0"),
            ),
            txn(transaction_id="buy", transaction_type="BUY", currency="USD", cash_amount=Decimal("-200")),
        ],
        fx_rate_to_chf=lambda currency: Decimal("0.8") if currency == "USD" else Decimal(1),
    )
    assert summary.contributed_capital_chf == Decimal("1400")
