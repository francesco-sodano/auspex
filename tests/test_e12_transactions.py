import base64
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import unittest

from api.auspex_api.app_users import InMemoryAppUserRepository
from api.auspex_api.portfolio import (
    CosmosPortfolioTransactionRepository,
    InMemoryMarketDataRepository,
    InMemoryPortfolioTransactionRepository,
    InMemorySecurityCatalog,
    InMemoryUniverseRepository,
    PortfolioTransaction,
    PortfolioService,
    ResolvedSecurity,
)
from api.auspex_api.owner_scoped import OwnerScope
from api.auspex_api.services import AuthorizationError, IdentityService


NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def principal_header(user_id, roles=None):
    payload = {
        "identityProvider": "aad",
        "userId": user_id,
        "userDetails": f"{user_id}@outlook.com",
        "userRoles": roles or ["anonymous", "authenticated"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def registration_payload():
    return {
        "adult_confirmed": True,
        "risk_disclosure_accepted": True,
        "advisory_disclaimer_accepted": True,
        "terms_accepted": True,
        "privacy_acknowledged": True,
    }


def onboarding_payload():
    return {
        "risk_profile": "Balanced",
        "base_currency": "USD",
        "investment_horizon": "12m",
        "suitability_acknowledged": True,
    }


def transaction_payload(request_id, transaction_type, **overrides):
    payload = {
        "client_request_id": request_id,
        "transaction_type": transaction_type,
        "event_date": "2026-07-22",
        "account_id": "primary",
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


class E12TransactionTests(unittest.TestCase):
    def test_transaction_routes_and_repository_are_owner_scoped(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")
        portfolio = (ROOT / "api" / "auspex_api" / "portfolio.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('route="transactions"', function_app)
        self.assertIn('route="transactions/{transaction_id}/correct"', function_app)
        self.assertIn('route="transaction_summary"', function_app)
        self.assertIn('route="portfolio_summary"', function_app)
        self.assertIn('route="stock/{code}/lookup"', function_app)
        self.assertIn("partition_key=scope.owner_user_sk", portfolio)
        self.assertIn("c.owner_user_sk = @owner_user_sk", portfolio)
        self.assertNotIn("enable_cross_partition_query", portfolio)

    def setUp(self):
        self.users = InMemoryAppUserRepository(clock=lambda: NOW)
        self.identity = IdentityService(self.users, clock=lambda: NOW)
        self.transactions = InMemoryPortfolioTransactionRepository()
        self.catalog = InMemorySecurityCatalog([
            ResolvedSecurity(
                security_sk=101,
                ticker="MSFT",
                isin="US5949181045",
                company_name="Microsoft Corporation",
                currency="USD",
                exchange="NASDAQ",
                gics_sector="Information Technology",
                country="US",
            ),
            ResolvedSecurity(
                security_sk=102,
                ticker="AAPL",
                isin="US0378331005",
                company_name="Apple Inc.",
                currency="USD",
                exchange="NASDAQ",
                gics_sector="Information Technology",
                country="US",
            ),
        ])
        self.universe = InMemoryUniverseRepository()
        self.market_data = InMemoryMarketDataRepository(
            quotes={"MSFT": {"price": "420.00", "currency": "USD", "as_of": "2026-07-21"}},
            fx_rates={
                ("USD", "CHF"): {"rate": "0.80000000", "as_of": "2026-07-21"},
            },
        )
        self.service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=self.universe,
            market_data=self.market_data,
            clock=lambda: NOW,
        )
        self.admin_header = principal_header(
            "admin-1", ["anonymous", "authenticated", "admin", "user"]
        )
        self.user_a = principal_header("user-a")
        self.user_b = principal_header("user-b")
        self.pending_user = principal_header("pending-user")
        self.identity.me(self.admin_header)
        self.approve_and_onboard(self.user_a)
        self.approve_and_onboard(self.user_b)
        self.identity.register(self.pending_user, registration_payload())

    def approve_and_onboard(self, header):
        pending, _ = self.identity.register(header, registration_payload())
        self.identity.review_user(self.admin_header, pending.user_sk, "approve")
        self.identity.onboard(header, onboarding_payload())

    def test_ledger_derives_signed_cash_and_positions(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("cash-1", "OPENING_CASH", amount="10000.00"),
        )
        buy, created = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "buy-1",
                "BUY",
                security_code="MSFT",
                quantity="2.5",
                price="400.00",
                fees="5.00",
            ),
        )
        sell, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "sell-1",
                "SELL",
                security_code="MSFT",
                quantity="0.5",
                price="420.00",
                fees="2.00",
            ),
        )

        summary = self.service.quick_summary(self.user_a)

        self.assertTrue(created)
        self.assertEqual(buy.cash_amount, "-1005.00")
        self.assertEqual(sell.cash_amount, "208.00")
        legacy_fields = {
            key: summary[key]
            for key in (
                "cash_by_currency", "positions", "transaction_count", "updated_on",
                "net_contributed_capital_by_currency", "total_fees_by_currency",
                "dividends_by_currency", "interest_by_currency", "total_value", "earnings",
            )
        }
        self.assertEqual(legacy_fields, {
            "cash_by_currency": {"USD": "9203.00"},
            "positions": [{"security_code": "MSFT", "quantity": "2"}],
            "transaction_count": 3,
            "updated_on": "2026-07-22",
            "net_contributed_capital_by_currency": {"USD": "10000.00"},
            "total_fees_by_currency": {"USD": "7.00"},
            "dividends_by_currency": {},
            "interest_by_currency": {},
            "total_value": {"status": "ready", "value_by_currency": {"USD": "10043.00"}, "reason": None},
            "earnings": {
                "status": "ready",
                "value_by_currency": {"USD": "43.00"},
                "reason": None,
            },
        })
        self.assertEqual(summary["reporting_currency"], "USD")
        self.assertEqual(summary["cash_total"], "9203.00")
        self.assertEqual(summary["net_contributed_capital_total"], "10000.00")
        self.assertEqual(summary["total_fees_total"], "7.00")
        self.assertEqual(summary["currency_exposure"], [
            {"name": "USD", "market_value_base": "10043.00", "weight": "1"},
        ])
        self.assertEqual(summary["allocation"]["cash_value"], "9203.00")
        self.assertEqual(summary["allocation"]["stocks_value"], "840.00")
        self.assertAlmostEqual(
            Decimal(summary["allocation"]["cash_weight"]),
            Decimal("9203") / Decimal("10043"),
        )
        self.assertAlmostEqual(
            Decimal(summary["allocation"]["stocks_weight"]),
            Decimal("840") / Decimal("10043"),
        )

    def test_ledger_usd_reporting_splits_portfolio_by_underlying_currency(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "chf-cash",
                "OPENING_CASH",
                currency="CHF",
                amount="1000",
                fx_rate_to_base="1.25",
            ),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "usd-stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="2",
                price="400",
            ),
        )

        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(summary["reporting_currency"], "USD")
        self.assertEqual(summary["total_value"]["value_by_currency"], {"USD": "2090.00"})
        self.assertEqual(summary["currency_exposure"], [
            {
                "name": "CHF",
                "market_value_base": "1250.00",
                "weight": "0.5980861244019138755980861244",
            },
            {
                "name": "USD",
                "market_value_base": "840.00",
                "weight": "0.4019138755980861244019138756",
            },
        ])
        self.assertEqual(summary["allocation"], {
            "cash_value": "1250.00",
            "stocks_value": "840.00",
            "complete": True,
            "reason": None,
            "cash_weight": "0.5980861244019138755980861244",
            "stocks_weight": "0.4019138755980861244019138756",
        })

    def test_ledger_summary_separates_capital_income_costs_and_withdrawals(self):
        for payload in [
            transaction_payload("opening-cash", "OPENING_CASH", amount="10000"),
            transaction_payload("deposit", "DEPOSIT", amount="500", fees="2"),
            transaction_payload("withdrawal", "WITHDRAWAL", amount="100", fees="1"),
            transaction_payload(
                "opening-stock",
                "OPENING_POSITION",
                security_code="AAPL",
                quantity="2",
                price="200",
            ),
            transaction_payload(
                "dividend", "DIVIDEND", security_code="AAPL", amount="50", fees="5"
            ),
            transaction_payload("interest", "INTEREST", amount="10"),
        ]:
            self.service.create_transaction(self.user_a, payload)

        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(summary["cash_by_currency"], {"USD": "10452.00"})
        self.assertEqual(
            summary["net_contributed_capital_by_currency"],
            {"USD": "10800.00"},
        )
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "8.00"})
        self.assertEqual(summary["dividends_by_currency"], {"USD": "50.00"})
        self.assertEqual(summary["interest_by_currency"], {"USD": "10.00"})
        self.assertNotIn("withdrawals_by_currency", summary)

    def test_standalone_fee_amount_is_included_in_total_costs(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="100"),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload("fee", "FEE", amount="10"),
        )

        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(summary["cash_by_currency"], {"USD": "90.00"})
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "10.00"})

    def test_opening_position_costs_are_linked_without_debiting_current_cash(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="100"),
        )
        opening, created = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position-costs",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="2",
                price="400",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "5", "currency": "USD"},
                    {"category": "TRANSACTION_TAX", "amount": "2", "currency": "USD"},
                ],
            ),
        )

        replay, replay_created = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position-costs",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="2.00",
                price="400.00",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "5.00", "currency": "USD"},
                    {"category": "TRANSACTION_TAX", "amount": "2.00", "currency": "USD"},
                ],
            ),
        )
        rows = self.service.list_transactions(self.user_a)
        linked = [row for row in rows if row.linked_transaction_id == opening.transaction_id]
        summary = self.service.quick_summary(self.user_a)

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.transaction_id, opening.transaction_id)
        self.assertEqual(summary["cash_by_currency"], {"USD": "100.00"})
        self.assertEqual(summary["net_contributed_capital_by_currency"], {"USD": "907.00"})
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "7.00"})
        self.assertEqual({row.cost_category for row in linked}, {"BROKER_COMMISSION", "TRANSACTION_TAX"})
        self.assertTrue(all(not row.affects_cash and row.cash_amount == "0.00" for row in linked))

    def test_buy_and_sell_store_gross_cash_separately_from_linked_costs(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="2000"),
        )
        buy, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "buy-with-costs", "BUY", security_code="MSFT", quantity="2", price="400",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "5", "currency": "USD"},
                ],
            ),
        )
        sell, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "sell-with-costs", "SELL", security_code="MSFT", quantity="0.5", price="420",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "2", "currency": "USD"},
                    {"category": "TRANSACTION_TAX", "amount": "1", "currency": "USD"},
                ],
            ),
        )

        rows = self.service.list_transactions(self.user_a)
        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(buy.gross_amount, "800.00")
        self.assertEqual(buy.cash_amount, "-800.00")
        self.assertEqual(sell.gross_amount, "210.00")
        self.assertEqual(sell.cash_amount, "210.00")
        self.assertEqual(summary["cash_by_currency"], {"USD": "1402.00"})
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "8.00"})
        self.assertEqual(len([row for row in rows if row.linked_transaction_id]), 3)

    def test_standalone_custody_fee_and_vat_are_separate_linked_costs(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="100"),
        )
        fee, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "custody-fee", "FEE", amount="10", cost_category="CUSTODY_FEE",
                cost_components=[
                    {"category": "VAT", "amount": "0.81", "currency": "USD"},
                ],
            ),
        )

        rows = self.service.list_transactions(self.user_a)
        vat = next(row for row in rows if row.linked_transaction_id == fee.transaction_id)
        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(fee.cost_category, "CUSTODY_FEE")
        self.assertEqual(fee.gross_amount, "10.00")
        self.assertEqual(fee.source_currency, "USD")
        self.assertEqual(fee.source_amount, "10.00")
        self.assertEqual(vat.cost_category, "VAT")
        self.assertEqual(summary["cash_by_currency"], {"USD": "89.19"})
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "10.81"})

    def test_dividend_preserves_gross_withholding_fx_and_net_settlement(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position", "OPENING_POSITION",
                security_code="MSFT", quantity="2", price="300",
            ),
        )
        dividend, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "cross-currency-dividend", "DIVIDEND", security_code="MSFT",
                amount="25", settlement_currency="CHF", fx_rate_to_settlement="0.8",
                cost_components=[
                    {
                        "category": "WITHHOLDING_TAX", "amount": "3.75",
                        "currency": "USD", "fx_rate_to_settlement": "0.8",
                    },
                ],
            ),
        )

        rows = self.service.list_transactions(self.user_a)
        withholding = next(row for row in rows if row.linked_transaction_id == dividend.transaction_id)
        summary = self.service.quick_summary(self.user_a)

        self.assertEqual(dividend.source_currency, "USD")
        self.assertEqual(dividend.source_amount, "25.00")
        self.assertEqual(dividend.gross_amount, "20.00")
        self.assertEqual(dividend.currency, "CHF")
        self.assertEqual(dividend.cash_amount, "20.00")
        self.assertEqual(withholding.source_amount, "3.75")
        self.assertEqual(withholding.gross_amount, "3.00")
        self.assertEqual(withholding.cash_amount, "-3.00")
        self.assertEqual(summary["cash_by_currency"], {"CHF": "17.00"})
        self.assertEqual(summary["dividends_by_currency"], {"CHF": "20.00"})
        self.assertEqual(summary["total_fees_by_currency"], {"CHF": "3.00"})

    def test_client_request_replay_converges_and_conflict_fails(self):
        payload = transaction_payload("deposit-1", "DEPOSIT", amount="250.00")

        first, created = self.service.create_transaction(self.user_a, payload)
        replay, replay_created = self.service.create_transaction(self.user_a, payload)

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.transaction_id, replay.transaction_id)
        with self.assertRaises(ValueError):
            self.service.create_transaction(
                self.user_a,
                transaction_payload("deposit-1", "DEPOSIT", amount="500.00"),
            )

    def test_correction_preserves_audit_rows_and_replaces_ledger_effect(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="10000"),
        )
        original, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "buy-original", "BUY", security_code="MSFT",
                quantity="2", price="400", fees="5",
            ),
        )

        correction, created = self.service.correct_transaction(
            self.user_a,
            original.transaction_id,
            transaction_payload(
                "buy-correction", "BUY", security_code="MSFT",
                quantity="1.5", price="400", fees="5",
            ),
        )
        replay, replay_created = self.service.correct_transaction(
            self.user_a,
            original.transaction_id,
            transaction_payload(
                "buy-correction", "BUY", security_code="MSFT",
                quantity="1.50", price="400.00", fees="5.00",
            ),
        )

        summary = self.service.quick_summary(self.user_a)
        effective_rows = self.service.list_transactions(self.user_a)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.transaction_id, correction.transaction_id)
        self.assertEqual(correction.corrects_transaction_id, original.transaction_id)
        self.assertEqual(len(effective_rows), 2)
        self.assertNotIn(original.transaction_id, {
            row.transaction_id for row in effective_rows
        })
        self.assertEqual(summary["cash_by_currency"], {"USD": "9395.00"})
        self.assertEqual(summary["positions"], [
            {"security_code": "MSFT", "quantity": "1.5"},
        ])
        self.assertEqual(summary["transaction_count"], 2)

    def test_correction_supersedes_parent_and_all_linked_costs(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="1000"),
        )
        original, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "linked-buy", "BUY", security_code="MSFT", quantity="2", price="400",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "5", "currency": "USD"},
                    {"category": "TRANSACTION_TAX", "amount": "2", "currency": "USD"},
                ],
            ),
        )

        correction, _ = self.service.correct_transaction(
            self.user_a,
            original.transaction_id,
            transaction_payload(
                "linked-buy-correction", "BUY", security_code="MSFT", quantity="1", price="400",
                cost_components=[
                    {"category": "BROKER_COMMISSION", "amount": "3", "currency": "USD"},
                ],
            ),
        )

        effective = self.service.list_transactions(self.user_a)
        summary = self.service.quick_summary(self.user_a)
        self.assertEqual(len(effective), 3)
        self.assertEqual(
            [row.cost_category for row in effective if row.linked_transaction_id == correction.transaction_id],
            ["BROKER_COMMISSION"],
        )
        self.assertEqual(summary["cash_by_currency"], {"USD": "597.00"})
        self.assertEqual(summary["total_fees_by_currency"], {"USD": "3.00"})

    def test_correction_is_owner_scoped_repeatable_and_state_validated(self):
        opening, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload("opening", "OPENING_CASH", amount="1000"),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "buy", "BUY", security_code="MSFT", quantity="2", price="400",
            ),
        )

        with self.assertRaisesRegex(ValueError, "transaction was not found"):
            self.service.correct_transaction(
                self.user_b,
                opening.transaction_id,
                transaction_payload("cross-owner", "OPENING_CASH", amount="2000"),
            )
        with self.assertRaisesRegex(ValueError, "insufficient cash"):
            self.service.correct_transaction(
                self.user_a,
                opening.transaction_id,
                transaction_payload("invalid-correction", "OPENING_CASH", amount="500"),
            )
        self.assertEqual(len(self.service.list_transactions(self.user_a)), 2)

        correction, _ = self.service.correct_transaction(
            self.user_a,
            opening.transaction_id,
            transaction_payload("valid-correction", "OPENING_CASH", amount="1200"),
        )
        with self.assertRaisesRegex(ValueError, "already corrected"):
            self.service.correct_transaction(
                self.user_a,
                opening.transaction_id,
                transaction_payload("second-correction", "OPENING_CASH", amount="1300"),
            )
        second_correction, created = self.service.correct_transaction(
            self.user_a,
            correction.transaction_id,
            transaction_payload("correction-chain", "OPENING_CASH", amount="1300"),
        )
        self.assertTrue(created)
        self.assertEqual(second_correction.corrects_transaction_id, correction.transaction_id)
        effective = self.service.list_transactions(self.user_a)
        self.assertEqual(
            [row.transaction_id for row in effective if row.transaction_type == "OPENING_CASH"],
            [second_correction.transaction_id],
        )

    def test_replay_normalizes_equivalent_decimal_spellings(self):
        first, created = self.service.create_transaction(
            self.user_a,
            transaction_payload("decimal-replay", "DEPOSIT", amount="250", fees="0"),
        )
        replay, replay_created = self.service.create_transaction(
            self.user_a,
            transaction_payload("decimal-replay", "DEPOSIT", amount="250.00", fees="0.00"),
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.transaction_id, replay.transaction_id)

    def test_security_transaction_is_resolved_and_onboards_universe_once(self):
        payload = transaction_payload(
            "opening-msft",
            "OPENING_POSITION",
            security_code="us5949181045",
            quantity="2",
            price="400",
            currency="CHF",
        )

        first, created = self.service.create_transaction(self.user_a, payload)
        replay, replay_created = self.service.create_transaction(self.user_a, payload)
        canonical_currency_replay, canonical_currency_created = self.service.create_transaction(
            self.user_a,
            {**payload, "currency": "USD"},
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertFalse(canonical_currency_created)
        self.assertEqual(first.security_sk, 101)
        self.assertEqual(first.security_code, "MSFT")
        self.assertEqual(first.currency, "USD")
        self.assertEqual(replay.transaction_id, first.transaction_id)
        self.assertEqual(canonical_currency_replay.transaction_id, first.transaction_id)
        self.assertEqual(self.catalog.resolve_calls, ["US5949181045"])
        self.assertEqual(self.universe.symbols(), ["MSFT"])

    def test_replay_retries_universe_onboarding_after_post_write_failure(self):
        class FlakyUniverse:
            def __init__(self):
                self.attempts = 0
                self.onboarded = []

            def onboard(self, security):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("universe unavailable")
                self.onboarded.append(security.ticker)

        universe = FlakyUniverse()
        service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=universe,
            market_data=self.market_data,
            clock=lambda: NOW,
        )
        payload = transaction_payload(
            "retry-universe",
            "OPENING_POSITION",
            security_code="MSFT",
            quantity="1",
            price="400",
        )

        with self.assertRaisesRegex(RuntimeError, "universe unavailable"):
            service.create_transaction(self.user_a, payload)
        replay, created = service.create_transaction(self.user_a, payload)

        self.assertFalse(created)
        self.assertEqual(replay.security_code, "MSFT")
        self.assertEqual(universe.onboarded, ["MSFT"])

    def test_replay_migrates_legacy_security_transaction_metadata(self):
        payload = transaction_payload(
            "legacy-security",
            "OPENING_POSITION",
            security_code="MSFT",
            quantity="1",
            price="400",
        )
        scope = self.service._scope(self.user_a)
        legacy = PortfolioTransaction.from_payload(scope.owner_user_sk, payload, now=NOW)
        self.transactions.create(scope, legacy)

        migrated, created = self.service.create_transaction(self.user_a, payload)

        self.assertFalse(created)
        self.assertEqual(migrated.security_sk, 101)
        self.assertEqual(migrated.security_name, "Microsoft Corporation")
        self.assertIsNotNone(migrated.request_hash)
        self.assertEqual(self.universe.symbols(), ["MSFT"])

    def test_unknown_security_is_rejected_before_transaction_write(self):
        with self.assertRaisesRegex(ValueError, "security was not found"):
            self.service.create_transaction(
                self.user_a,
                transaction_payload(
                    "unknown",
                    "BUY",
                    security_code="UNKNOWN",
                    quantity="1",
                    price="10",
                ),
            )

        self.assertEqual(self.service.list_transactions(self.user_a), [])

    def test_invalid_security_transaction_does_not_onboard_universe(self):
        with self.assertRaises(ValueError):
            self.service.create_transaction(
                self.user_a,
                transaction_payload(
                    "bad-quantity",
                    "BUY",
                    security_code="MSFT",
                    quantity="0",
                    price="10",
                ),
            )

        self.assertEqual(self.universe.symbols(), [])

    def test_cash_outflows_and_buys_cannot_exceed_available_cash(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="1000"),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload("withdraw", "WITHDRAWAL", amount="100", fees="1"),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload("fee", "FEE", amount="10"),
        )

        for payload in [
            transaction_payload("withdraw-too-much", "WITHDRAWAL", amount="890"),
            transaction_payload("fee-too-much", "FEE", amount="890"),
            transaction_payload(
                "buy-too-much", "BUY", security_code="MSFT",
                quantity="3", price="300", fees="1",
            ),
        ]:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                ValueError, "insufficient cash"
            ):
                self.service.create_transaction(self.user_a, payload)

    def test_sell_is_limited_to_current_holding(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position", "OPENING_POSITION",
                security_code="MSFT", quantity="5", price="300",
            ),
        )
        sold, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "sell", "SELL", security_code="MSFT",
                quantity="4", price="420",
            ),
        )

        self.assertEqual(sold.quantity, "4")
        with self.assertRaisesRegex(ValueError, "exceeds held quantity"):
            self.service.create_transaction(
                self.user_a,
                transaction_payload(
                    "sell-too-much", "SELL", security_code="MSFT",
                    quantity="2", price="420",
                ),
            )

    def test_concurrent_sell_is_revalidated_after_revision_conflict(self):
        scope = OwnerScope("owner-concurrency")
        security = self.catalog.resolve("MSFT")
        opening = PortfolioTransaction.from_payload(
            scope.owner_user_sk,
            transaction_payload(
                "opening", "OPENING_POSITION", security_code="MSFT",
                quantity="5", price="300",
            ),
            now=NOW - dt.timedelta(minutes=2),
            resolved_security=security,
            base_currency="USD",
        )
        winner = PortfolioTransaction.from_payload(
            scope.owner_user_sk,
            transaction_payload(
                "winner", "SELL", security_code="MSFT",
                quantity="3", price="420",
            ),
            now=NOW - dt.timedelta(minutes=1),
            resolved_security=security,
            base_currency="USD",
        )
        candidate = PortfolioTransaction.from_payload(
            scope.owner_user_sk,
            transaction_payload(
                "candidate", "SELL", security_code="MSFT",
                quantity="4", price="420",
            ),
            now=NOW,
            resolved_security=security,
            base_currency="USD",
        )

        class ConflictContainer:
            def __init__(self):
                self.documents = [opening.to_document()]
                self.revision = {
                    "id": "_ledger_revision", "owner_user_sk": scope.owner_user_sk,
                    "kind": "ledger_revision", "version": 1, "_etag": "etag-1",
                }
                self.batch_attempts = 0
                self.events = []

            def read_item(self, item, partition_key):
                if item == "_ledger_revision":
                    self.events.append("revision")
                    return dict(self.revision)
                match = next((row for row in self.documents if row["id"] == item), None)
                if match is not None:
                    return match
                error = RuntimeError("not found")
                error.status_code = 404
                raise error

            def query_items(self, **_):
                self.events.append("ledger")
                return list(self.documents)

            def execute_item_batch(self, **_):
                self.batch_attempts += 1
                self.documents.append(winner.to_document())
                self.revision.update({"version": 2, "_etag": "etag-2"})
                error = RuntimeError("revision changed")
                error.status_code = 412
                raise error

        container = ConflictContainer()
        repository = CosmosPortfolioTransactionRepository(container)

        with self.assertRaisesRegex(ValueError, "exceeds held quantity"):
            repository.create(scope, candidate, PortfolioService._validate_state)

        self.assertEqual(container.batch_attempts, 1)
        self.assertEqual(container.events[:2], ["revision", "ledger"])

    def test_dividend_requires_a_currently_held_security(self):
        with self.assertRaisesRegex(ValueError, "held security"):
            self.service.create_transaction(
                self.user_a,
                transaction_payload(
                    "dividend-without-holding", "DIVIDEND",
                    security_code="MSFT", amount="25",
                ),
            )

        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position", "OPENING_POSITION",
                security_code="MSFT", quantity="2", price="300",
            ),
        )
        dividend, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "dividend", "DIVIDEND", security_code="MSFT", amount="25",
            ),
        )

        self.assertEqual(dividend.security_sk, 101)
        self.assertEqual(dividend.security_code, "MSFT")

    def test_opening_balances_are_unique_and_must_precede_activity(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("opening-cash", "OPENING_CASH", amount="1000"),
        )
        with self.assertRaisesRegex(ValueError, "opening cash"):
            self.service.create_transaction(
                self.user_a,
                transaction_payload("opening-cash-again", "OPENING_CASH", amount="100"),
            )

        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "opening-position", "OPENING_POSITION",
                security_code="MSFT", quantity="2", price="300",
            ),
        )
        with self.assertRaisesRegex(ValueError, "opening position"):
            self.service.create_transaction(
                self.user_a,
                transaction_payload(
                    "opening-position-again", "OPENING_POSITION",
                    security_code="MSFT", quantity="1", price="300",
                ),
            )

    def test_user_fx_rate_is_stored_and_used_for_contributed_capital(self):
        transaction, _ = self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "chf-deposit", "DEPOSIT", currency="CHF", amount="100",
                fx_rate_to_base="1.20",
            ),
        )

        summary = self.service.portfolio_summary(self.user_a)

        self.assertEqual(transaction.base_currency, "USD")
        self.assertEqual(transaction.fx_rate_to_base, "1.2")
        self.assertEqual(summary["net_contributed_capital_base"], "120.00")

    def test_portfolio_summary_values_cash_and_positions_in_profile_currency(self):
        self.service.create_transaction(
            self.user_a,
            transaction_payload("cash", "OPENING_CASH", amount="1000"),
        )
        self.service.create_transaction(
            self.user_a,
            transaction_payload(
                "stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="2",
                price="400",
            ),
        )

        summary = self.service.portfolio_summary(self.user_a)

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["base_currency"], "USD")
        self.assertEqual(summary["total_cash_base"], "1000.00")
        self.assertEqual(summary["total_stocks_base"], "840.00")
        self.assertEqual(summary["total_value_base"], "1840.00")
        self.assertEqual(summary["net_contributed_capital_base"], "1800.00")
        self.assertEqual(summary["total_earnings_base"], "40.00")
        self.assertEqual(summary["holdings"][0]["company_name"], "Microsoft Corporation")
        self.assertEqual(summary["holdings"][0]["price_currency"], "USD")
        self.assertEqual(summary["exposures"]["sector"], [{
            "name": "Information Technology",
            "market_value_base": "840.00",
            "weight": summary["holdings"][0]["weight"],
        }])
        self.assertEqual(summary["exposures"]["country"][0]["name"], "US")
        self.assertEqual(summary["exposures"]["currency"][0]["name"], "USD")

    def test_portfolio_summary_withholds_partial_value_when_quote_is_missing(self):
        self.market_data = InMemoryMarketDataRepository()
        service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=self.universe,
            market_data=self.market_data,
            clock=lambda: NOW,
        )
        service.create_transaction(
            self.user_a,
            transaction_payload(
                "stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="1",
                price="400",
            ),
        )

        summary = service.portfolio_summary(self.user_a)
        ledger_summary = service.quick_summary(self.user_a)

        self.assertEqual(summary["status"], "pending_ingestion")
        self.assertIsNone(summary["total_value_base"])
        self.assertEqual(summary["valued_total_base"], "0.00")
        self.assertEqual(summary["holdings"][0]["valuation_status"], "missing_price")
        self.assertEqual(summary["coverage"]["missing_prices"], ["MSFT"])
        self.assertEqual(
            ledger_summary["total_value"]["value_by_currency"],
            {"USD": "0.00"},
        )
        self.assertEqual(ledger_summary["coverage"]["missing_prices"], ["MSFT"])
        self.assertEqual(ledger_summary["assets"][1], {
            "asset_type": "stock",
            "ticker": "MSFT",
            "name": "Microsoft Corporation",
            "quantity": "1",
            "price_currency": None,
            "latest_price": None,
            "current_value": None,
            "weight": None,
            "valuation_status": "missing_price",
        })
        self.assertFalse(ledger_summary["allocation"]["complete"])
        self.assertEqual(
            ledger_summary["allocation"]["reason"],
            "incomplete_coverage",
        )

    def test_missing_position_fx_does_not_hide_convertible_cash(self):
        market_data = InMemoryMarketDataRepository(
            quotes={"MSFT": {"price": "420.00", "currency": "EUR", "as_of": "2026-07-21"}},
        )
        service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=self.universe,
            market_data=market_data,
            clock=lambda: NOW,
        )
        service.create_transaction(
            self.user_a,
            transaction_payload("cash", "OPENING_CASH", amount="1000"),
        )
        service.create_transaction(
            self.user_a,
            transaction_payload(
                "stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="1",
                price="400",
            ),
        )

        summary = service.portfolio_summary(self.user_a)

        self.assertEqual(summary["status"], "pending_ingestion")
        self.assertEqual(summary["total_cash_base"], "1000.00")
        self.assertIsNone(summary["total_value_base"])
        self.assertEqual(summary["coverage"]["missing_fx"], ["EUR/USD"])

    def test_portfolio_summary_marks_old_complete_prices_stale(self):
        market_data = InMemoryMarketDataRepository(
            quotes={"MSFT": {"price": "420.00", "currency": "USD", "as_of": "2026-07-01"}},
        )
        service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=self.universe,
            market_data=market_data,
            clock=lambda: NOW,
        )
        service.create_transaction(
            self.user_a,
            transaction_payload(
                "stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="1",
                price="400",
            ),
        )

        summary = service.portfolio_summary(self.user_a)

        self.assertEqual(summary["status"], "stale")
        self.assertEqual(summary["total_value_base"], "420.00")

    def test_portfolio_summary_marks_old_fx_stale_even_with_fresh_quote(self):
        self.identity.onboard(
            self.user_a,
            {
                "risk_profile": "Balanced",
                "base_currency": "CHF",
                "investment_horizon": "12m",
                "suitability_acknowledged": True,
            },
        )
        market_data = InMemoryMarketDataRepository(
            quotes={"MSFT": {"price": "420.00", "currency": "USD", "as_of": "2026-07-21"}},
            fx_rates={("USD", "CHF"): {"rate": "0.8", "as_of": "2026-07-01"}},
        )
        service = PortfolioService(
            self.identity,
            self.transactions,
            security_catalog=self.catalog,
            universe=self.universe,
            market_data=market_data,
            clock=lambda: NOW,
        )
        service.create_transaction(
            self.user_a,
            transaction_payload(
                "stock",
                "OPENING_POSITION",
                security_code="MSFT",
                quantity="1",
                price="400",
                event_date="2026-07-01",
            ),
        )

        summary = service.portfolio_summary(self.user_a)

        self.assertEqual(summary["status"], "stale")
        self.assertEqual(summary["valuation_as_of"], "2026-07-01")

    def test_portfolio_summary_is_owner_scoped(self):
        self.service.create_transaction(
            self.user_b,
            transaction_payload("private-cash", "DEPOSIT", amount="250"),
        )

        self.assertEqual(self.service.portfolio_summary(self.user_a)["status"], "empty")
        self.assertEqual(self.service.portfolio_summary(self.user_b)["total_cash_base"], "250.00")

    def test_transactions_are_structurally_owner_scoped(self):
        self.service.create_transaction(
            self.user_b,
            transaction_payload("private-1", "DEPOSIT", amount="100.00"),
        )

        self.assertEqual(self.service.list_transactions(self.user_a), [])
        user_b_rows = self.service.list_transactions(self.user_b)
        self.assertEqual(len(user_b_rows), 1)
        self.assertNotIn("owner_user_sk", user_b_rows[0].public_payload())

    def test_pending_user_cannot_read_or_write_transactions(self):
        with self.assertRaises(AuthorizationError):
            self.service.list_transactions(self.pending_user)
        with self.assertRaises(AuthorizationError):
            self.service.create_transaction(
                self.pending_user,
                transaction_payload("blocked-1", "DEPOSIT", amount="100.00"),
            )

    def test_invalid_transaction_payloads_fail_closed(self):
        invalid_payloads = [
            transaction_payload("bad-type", "TRANSFER", amount="100.00"),
            transaction_payload("bad-buy", "BUY", security_code="MSFT", quantity="1"),
            transaction_payload("bad-cash", "DEPOSIT", amount="-1"),
            transaction_payload("bad-date", "FEE", amount="1", event_date="07/22/2026"),
            transaction_payload("bad-currency", "DEPOSIT", amount="1", currency="BTC"),
            transaction_payload("future-date", "DEPOSIT", amount="1", event_date="2026-07-23"),
            transaction_payload("too-precise-money", "DEPOSIT", amount="1.001"),
            transaction_payload("too-precise-quantity", "BUY", security_code="MSFT", quantity="0.000000001", price="1"),
            transaction_payload("too-large-amount", "DEPOSIT", amount="1000000000000"),
            transaction_payload("too-large-notional", "BUY", security_code="MSFT", quantity="1000000000", price="1000000000"),
            transaction_payload("fees-push-over-limit", "WITHDRAWAL", amount="999999999999.99", fees="0.01"),
            transaction_payload("nan-amount", "DEPOSIT", amount="NaN"),
            transaction_payload("infinite-fees", "DEPOSIT", amount="10", fees="Infinity"),
            transaction_payload("opening-with-fee", "OPENING_CASH", amount="10", fees="1"),
            transaction_payload("fee-with-fee", "FEE", amount="10", fees="1"),
            transaction_payload("unused-fx", "INTEREST", currency="CHF", amount="10", fx_rate_to_base="1.2"),
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.create_transaction(self.user_a, payload)


if __name__ == "__main__":
    unittest.main()
