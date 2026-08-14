"""Unit tests for the portfolio read adapter over the event-sourced ledger.

Mutation is owned by PortfolioLedgerService; this adapter retains a narrow
read-only protocol so scoring code cannot mutate financial history.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from auspex.portfolio.adapter import OwnerResolutionError, PortfolioAdapter
from auspex.portfolio.mapping import IdentityMappingConfig, PortfolioMappingConfig, TransactionsMappingConfig


class WriteAttemptError(AssertionError):
    """Raised by the fake container if anything tries to call a write method."""


class StrictReadOnlyContainer:
    """A fake container that only implements `query_items`/`read_item` — no
    write method exists at all, so a write attempt raises `AttributeError`,
    not silently succeeding. Additionally raises `WriteAttemptError` from
    common write method names to give a clear failure message if someone
    ever adds one."""

    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents
        self.query_call_count = 0

    def query_items(self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None):
        self.query_call_count += 1
        docs = list(self._documents)

        async def _iterate():
            for doc in docs:
                yield doc

        return _iterate()

    async def read_item(self, item: str, partition_key: str) -> dict:
        for doc in self._documents:
            if doc.get("id") == item:
                return doc
        raise KeyError(item)

    def __getattr__(self, name: str):
        if name in ("upsert_item", "create_item", "replace_item", "delete_item", "patch_item"):
            raise WriteAttemptError(f"PortfolioAdapter attempted a write via {name!r} — this must never happen")
        raise AttributeError(name)


class StrictReadOnlyDatabase:
    def __init__(self, containers: dict[str, list[dict]]) -> None:
        self._containers = containers
        self.containers_created: list[StrictReadOnlyContainer] = []

    def get_container_client(self, name: str) -> StrictReadOnlyContainer:
        container = StrictReadOnlyContainer(self._containers.get(name, []))
        self.containers_created.append(container)
        return container


def make_mapping(owner_user_sk: str = "owner-1-sk") -> PortfolioMappingConfig:
    return PortfolioMappingConfig(
        transactions=TransactionsMappingConfig(
            container="portfolio_transactions", partition_key_field="owner_user_sk"
        ),
        owner_user_sk=owner_user_sk,
        identity_mapping=None,
    )


def usd_to_chf(currency: str) -> Decimal | None:
    return Decimal("0.8") if currency == "USD" else Decimal(1) if currency == "CHF" else None


def txn(**overrides) -> dict:
    # cash_amount defaults to "0": most fixtures below only exercise position
    # derivation. Cash-derivation-specific tests set cash_amount/currency
    # explicitly (they need an FX resolver for any non-CHF non-zero balance).
    base = {
        "id": "txn-1",
        "transaction_id": "txn-1",
        "owner_user_sk": "owner-1-sk",
        "transaction_type": "BUY",
        "event_date": "2026-01-05",
        "currency": "USD",
        "security_code": "NVDA",
        "quantity": "10",
        "price": "150",
        "cash_amount": "0",
        "fees": "0",
        "created_at": "2026-01-05T10:00:00Z",
    }
    base.update(overrides)
    return base


class TestPortfolioAdapterIsReadOnly:
    @pytest.mark.asyncio
    async def test_read_snapshot_never_calls_a_write_method(self):
        db = StrictReadOnlyDatabase({"portfolio_transactions": [txn()]})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        # Would raise WriteAttemptError immediately if the adapter ever touched a write method.
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].ticker == "NVDA"

    @pytest.mark.asyncio
    async def test_only_query_items_is_ever_invoked_for_transactions(self):
        db = StrictReadOnlyDatabase({"portfolio_transactions": [txn()]})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        await adapter.read_snapshot(date(2026, 8, 8))
        assert len(db.containers_created) == 1
        assert db.containers_created[0].query_call_count == 1

    @pytest.mark.asyncio
    async def test_revision_sentinel_excluded_via_query_filter(self):
        db = StrictReadOnlyDatabase({"portfolio_transactions": [txn()]})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        await adapter.read_snapshot(date(2026, 8, 8))
        # The fake ignores the WHERE clause and just returns everything, so this
        # asserts the adapter *constructs* a filter excluding the revision id
        # rather than trusting the container to already exclude it.
        container = db.containers_created[0]
        assert container.query_call_count == 1


class TestReadSnapshot:
    @pytest.mark.asyncio
    async def test_holdings_derived_from_buy_events(self):
        db = StrictReadOnlyDatabase({"portfolio_transactions": [txn()]})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert snapshot.lot_level is True
        holding = snapshot.holdings[0]
        assert holding.quantity == Decimal(10)
        assert holding.cost_basis_usd == Decimal(1500)
        assert holding.lot_id == "txn-1"

    @pytest.mark.asyncio
    async def test_sell_reduces_holding_fifo(self):
        docs = [
            txn(transaction_id="t1", id="t1", quantity="10", price="100", created_at="2026-01-01T00:00:00Z"),
            txn(
                transaction_id="t2",
                id="t2",
                transaction_type="SELL",
                quantity="4",
                price="120",
                event_date="2026-01-10",
                created_at="2026-01-10T00:00:00Z",
            ),
        ]
        db = StrictReadOnlyDatabase({"portfolio_transactions": docs})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].quantity == Decimal(6)

    @pytest.mark.asyncio
    async def test_cash_chf_aggregates_chf_cash_events(self):
        docs = [txn(transaction_type="OPENING_CASH", currency="CHF", cash_amount="5000", security_code=None)]
        db = StrictReadOnlyDatabase({"portfolio_transactions": docs})
        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=usd_to_chf)
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert snapshot.cash_chf == Decimal(5000)

    @pytest.mark.asyncio
    async def test_cash_chf_uses_fx_resolver_for_non_chf(self):
        docs = [txn(transaction_type="OPENING_CASH", currency="USD", cash_amount="1000", security_code=None)]
        db = StrictReadOnlyDatabase({"portfolio_transactions": docs})

        def resolver(ccy: str) -> Decimal | None:
            return Decimal("0.9") if ccy == "USD" else None

        adapter = PortfolioAdapter(db, make_mapping(), fx_rate_to_chf=resolver)
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert snapshot.cash_chf == Decimal("900.0")

    @pytest.mark.asyncio
    async def test_corrected_transaction_excluded(self):
        docs = [
            txn(transaction_id="t1", id="t1", quantity="10"),
            txn(
                transaction_id="t2",
                id="t2",
                corrects_transaction_id="t1",
                quantity="7",
                created_at="2026-01-06T00:00:00Z",
            ),
        ]
        db = StrictReadOnlyDatabase({"portfolio_transactions": docs})
        adapter = PortfolioAdapter(db, make_mapping())
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].quantity == Decimal(7)

    @pytest.mark.asyncio
    async def test_sample_holding_document_returns_raw_dict(self):
        raw = txn()
        db = StrictReadOnlyDatabase({"portfolio_transactions": [raw]})
        adapter = PortfolioAdapter(db, make_mapping())
        sample = await adapter.sample_holding_document()
        assert sample == raw

    @pytest.mark.asyncio
    async def test_sample_holding_document_none_when_empty(self):
        db = StrictReadOnlyDatabase({"portfolio_transactions": []})
        adapter = PortfolioAdapter(db, make_mapping())
        assert await adapter.sample_holding_document() is None


class TestOwnerResolution:
    @pytest.mark.asyncio
    async def test_single_active_owner_is_resolved_without_hardcoded_identity(self):
        mapping = PortfolioMappingConfig(
            transactions=TransactionsMappingConfig(
                container="portfolio_transactions", partition_key_field="owner_user_sk"
            ),
            owner_user_sk=None,
            identity_mapping=IdentityMappingConfig(container="app_users"),
        )
        db = StrictReadOnlyDatabase(
            {
                "app_users": [{"id": "identity", "status": "active", "user_sk": "resolved-owner-sk"}],
                "portfolio_transactions": [
                    txn(owner_user_sk="resolved-owner-sk", currency="CHF", cash_amount="0")
                ],
            }
        )
        adapter = PortfolioAdapter(db, mapping)
        assert await adapter.resolve_owner_user_sk() == "resolved-owner-sk"

    @pytest.mark.asyncio
    async def test_dynamic_identity_mapping_resolves_owner_user_sk(self):
        mapping = PortfolioMappingConfig(
            transactions=TransactionsMappingConfig(
                container="portfolio_transactions", partition_key_field="owner_user_sk"
            ),
            owner_user_sk=None,
            identity_mapping=IdentityMappingConfig(container="app_users", identity_key="aad-principal-1"),
        )
        db = StrictReadOnlyDatabase(
            {
                "app_users": [{"id": "aad-principal-1", "user_sk": "resolved-owner-sk"}],
                "portfolio_transactions": [txn(owner_user_sk="resolved-owner-sk")],
            }
        )
        adapter = PortfolioAdapter(db, mapping, fx_rate_to_chf=usd_to_chf)
        snapshot = await adapter.read_snapshot(date(2026, 8, 8))
        assert len(snapshot.holdings) == 1

    @pytest.mark.asyncio
    async def test_missing_identity_document_raises_owner_resolution_error(self):
        mapping = PortfolioMappingConfig(
            transactions=TransactionsMappingConfig(
                container="portfolio_transactions", partition_key_field="owner_user_sk"
            ),
            owner_user_sk=None,
            identity_mapping=IdentityMappingConfig(container="app_users", identity_key="missing-principal"),
        )
        db = StrictReadOnlyDatabase({"app_users": [], "portfolio_transactions": []})
        adapter = PortfolioAdapter(db, mapping)
        with pytest.raises(OwnerResolutionError):
            await adapter.read_snapshot(date(2026, 8, 8))
