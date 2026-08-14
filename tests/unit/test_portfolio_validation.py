"""Unit tests for portfolio binding validation (arc42 §6.3 step 11) over the
event-sourced ledger.

An unmapped ticker is a **hard failure**: a position Auspex cannot see is a
position it cannot advise on, and the owner would otherwise have no signal
that it was invisible.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile
from auspex.models.security import Security
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.mapping import PortfolioMappingConfig, TransactionsMappingConfig
from auspex.portfolio.validation import validate_portfolio_binding


class FakeContainer:
    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    def query_items(self, query: str, parameters=None, partition_key=None):
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


class FakeDatabase:
    def __init__(self, containers: dict[str, list[dict]]) -> None:
        self._containers = containers

    def get_container_client(self, name: str):
        return FakeContainer(self._containers.get(name, []))


def make_universe(tickers: list[str]) -> Universe:
    securities = [
        Security(
            id=f"sec-{t}",
            ticker=t,
            cik="0000000000",
            name=t,
            cohort="semi-compute",
            filer_profile=FilerProfile.DOMESTIC,
            investable=True,
        )
        for t in tickers
    ]
    return Universe(securities=securities)


def make_mapping() -> PortfolioMappingConfig:
    return PortfolioMappingConfig(
        transactions=TransactionsMappingConfig(
            container="portfolio_transactions", partition_key_field="owner_user_sk"
        ),
        owner_user_sk="owner-1-sk",
        identity_mapping=None,
    )


def txn(**overrides) -> dict:
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


class TestValidatePortfolioBinding:
    @pytest.mark.asyncio
    async def test_all_tickers_mapped_is_valid(self):
        universe = make_universe(["NVDA", "AMD"])
        db = FakeDatabase({"portfolio_transactions": [txn()]})
        adapter = PortfolioAdapter(db, make_mapping())
        result = await validate_portfolio_binding(adapter, universe, date(2026, 8, 8))
        assert result.is_valid
        assert result.unmapped_tickers == []

    @pytest.mark.asyncio
    async def test_unmapped_ticker_is_a_hard_failure(self):
        universe = make_universe(["NVDA"])  # AMD not in universe
        docs = [
            txn(),
            txn(transaction_id="txn-2", id="txn-2", security_code="AMD", quantity="5"),
        ]
        db = FakeDatabase({"portfolio_transactions": docs})
        adapter = PortfolioAdapter(db, make_mapping())
        result = await validate_portfolio_binding(adapter, universe, date(2026, 8, 8))
        assert not result.is_valid
        assert "AMD" in result.unmapped_tickers

    @pytest.mark.asyncio
    async def test_sample_document_captured_for_owner_confirmation(self):
        universe = make_universe(["NVDA"])
        raw = txn()
        db = FakeDatabase({"portfolio_transactions": [raw]})
        adapter = PortfolioAdapter(db, make_mapping())
        result = await validate_portfolio_binding(adapter, universe, date(2026, 8, 8))
        assert result.sample_document == raw

    @pytest.mark.asyncio
    async def test_empty_portfolio_has_no_unmapped_tickers(self):
        universe = make_universe(["NVDA"])
        db = FakeDatabase({"portfolio_transactions": []})
        adapter = PortfolioAdapter(db, make_mapping())
        result = await validate_portfolio_binding(adapter, universe, date(2026, 8, 8))
        assert result.is_valid
        assert result.snapshot.cash_chf == Decimal("0")
