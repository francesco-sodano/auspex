import datetime as dt
import json
from pathlib import Path
import sys
import unittest


CONNECTORS_ROOT = Path(__file__).resolve().parents[1] / "connectors"
sys.path.insert(0, str(CONNECTORS_ROOT))

from portfolio.connector import PortfolioConnector


class FakeControlPlane:
    def __init__(self, documents):
        self.documents = documents

    def list_portfolio_transactions(self):
        return self.documents


class FakeBronzeWriter:
    pass


class E12PortfolioConnectorTests(unittest.TestCase):
    def test_control_plane_excludes_private_ledger_revision(self):
        control_plane = (
            Path(__file__).resolve().parents[1]
            / "connectors" / "shared" / "control_plane.py"
        ).read_text(encoding="utf-8")

        self.assertIn("c.id != '_ledger_revision'", control_plane)

    def test_function_registry_requires_portfolio_schema_v5(self):
        function_app = (
            Path(__file__).resolve().parents[1]
            / "connectors" / "function_app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"portfolio": 5', function_app)
        self.assertEqual(
            function_app.count('"schema_version": source.get("schema_version")'),
            2,
        )

    def test_snapshot_is_deterministic_and_preserves_owner_scope(self):
        documents = [
            {
                "id": "tx-b",
                "transaction_id": "tx-b",
                "owner_user_sk": "owner-b",
                "client_request_id": "request-b",
                "account_id": "primary",
                "transaction_type": "DEPOSIT",
                "event_date": "2026-07-22",
                "currency": "USD",
                "security_sk": None,
                "security_code": None,
                "security_currency": None,
                "quantity": None,
                "price": None,
                "fees": "0.00",
                "cash_amount": "100.00",
                "base_currency": "USD",
                "fx_rate_to_base": None,
                "payload_hash": "b" * 64,
                "created_at": "2026-07-22T12:00:00+00:00",
                "_etag": "volatile",
            },
            {
                "id": "tx-a",
                "transaction_id": "tx-a",
                "owner_user_sk": "owner-a",
                "client_request_id": "request-a",
                "account_id": "primary",
                "transaction_type": "OPENING_CASH",
                "event_date": "2026-07-21",
                "currency": "CHF",
                "security_sk": None,
                "security_code": None,
                "security_currency": None,
                "quantity": None,
                "price": None,
                "fees": "0.00",
                "cash_amount": "50.00",
                "base_currency": "USD",
                "fx_rate_to_base": "1.20",
                "payload_hash": "a" * 64,
                "created_at": "2026-07-21T10:00:00+00:00",
            },
        ]
        connector = PortfolioConnector(FakeControlPlane(documents), FakeBronzeWriter())

        first = connector.fetch(None)
        second = connector.fetch(None)

        self.assertEqual(first.window, second.window)
        self.assertEqual(first.records[0]["record_type"], "snapshot_manifest")
        self.assertEqual(first.records[0]["transaction_count"], 2)
        self.assertEqual([row["transaction_id"] for row in first.records[1:]], ["tx-a", "tx-b"])
        self.assertEqual(first.records[1]["owner_user_sk"], "owner-a")
        self.assertEqual(first.records[1]["fx_rate_to_base"], "1.20")
        self.assertTrue(all(row["snapshot_id"] == first.records[0]["snapshot_id"] for row in first.records))
        self.assertNotIn("_etag", json.dumps(first.records))
        self.assertEqual(first.partition_date, "2026-07-22")

    def test_empty_ledger_emits_explicit_zero_row_snapshot(self):
        batch = PortfolioConnector(FakeControlPlane([]), FakeBronzeWriter()).fetch(None)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0]["record_type"], "snapshot_manifest")
        self.assertEqual(batch.records[0]["transaction_count"], 0)

    def test_snapshot_preserves_append_only_correction_linkage(self):
        connector = PortfolioConnector(FakeControlPlane([{
            "transaction_id": "replacement",
            "owner_user_sk": "owner-a",
            "client_request_id": "replace-request",
            "account_id": "primary",
            "transaction_type": "DEPOSIT",
            "event_date": "2026-07-22",
            "currency": "USD",
            "fees": "0.00",
            "cash_amount": "200.00",
            "base_currency": "USD",
            "fx_rate_to_base": None,
            "payload_hash": "c" * 64,
            "created_at": "2026-07-22T13:00:00+00:00",
            "corrects_transaction_id": "original",
        }]), FakeBronzeWriter())

        batch = connector.fetch(None)

        self.assertEqual(batch.records[1]["corrects_transaction_id"], "original")

    def test_snapshot_preserves_linked_cost_accounting_fields(self):
        connector = PortfolioConnector(FakeControlPlane([{
            "transaction_id": "cost-row",
            "owner_user_sk": "owner-a",
            "client_request_id": "cost-request",
            "account_id": "primary",
            "transaction_type": "FEE",
            "event_date": "2026-07-22",
            "currency": "CHF",
            "fees": "0.00",
            "cash_amount": "-3.00",
            "gross_amount": "3.00",
            "source_currency": "USD",
            "source_amount": "3.75",
            "fx_rate_to_settlement": "0.8",
            "linked_transaction_id": "dividend-row",
            "cost_category": "WITHHOLDING_TAX",
            "affects_cash": True,
            "payload_hash": "d" * 64,
            "created_at": "2026-07-22T13:00:00+00:00",
        }]), FakeBronzeWriter())

        transaction = connector.fetch(None).records[1]

        self.assertEqual(transaction["linked_transaction_id"], "dividend-row")
        self.assertEqual(transaction["cost_category"], "WITHHOLDING_TAX")
        self.assertEqual(transaction["source_amount"], "3.75")
        self.assertTrue(transaction["affects_cash"])


if __name__ == "__main__":
    unittest.main()