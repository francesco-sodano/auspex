"""Unit tests for bootstrap step 11's portfolio-binding confirmation gate
(arc42 §6.3 step 11).

Bootstrap runs unattended for 2.5-5 hours against the real, owner-owned
source ledger, so it must never silently proceed past an unreviewed
binding. These tests prove: (1) the mapped sample document and a binding
summary are always logged, and (2) continuation absolutely requires an
explicit ``confirmed=True`` — there is no default-true path anywhere.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from auspex.cli.bootstrap import BootstrapRunner, PortfolioBindingNotConfirmedError
from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile
from auspex.models.security import Security
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.mapping import PortfolioMappingConfig, TransactionsMappingConfig
from auspex.settings import Settings


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


def make_runner(tickers: list[str]) -> BootstrapRunner:
    return BootstrapRunner(universe=make_universe(tickers), context_factory=lambda d: None)


class TestConfirmationGate:
    @pytest.mark.asyncio
    async def test_unconfirmed_raises_and_does_not_return_a_result(self):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with pytest.raises(PortfolioBindingNotConfirmedError):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=False)

    @pytest.mark.asyncio
    async def test_confirmed_true_returns_the_binding_result(self):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        result = await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=True)

        assert result.is_valid
        assert result.sample_document == txn()

    @pytest.mark.asyncio
    async def test_confirmed_is_a_required_keyword_argument(self):
        """No default exists anywhere — omitting it entirely is a TypeError,
        not a silent proceed."""

        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with pytest.raises(TypeError):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8))  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_unconfirmed_still_raises_even_with_a_clean_binding(self):
        """The gate is unconditional — it doesn't only fire on validation
        problems. A clean, fully-mapped binding still requires explicit
        confirmation before bootstrap proceeds."""

        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])  # NVDA is mapped — no unmapped tickers

        with pytest.raises(PortfolioBindingNotConfirmedError):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=False)

    @pytest.mark.asyncio
    async def test_unmapped_tickers_still_logged_as_error_even_when_confirmed(self):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["AMD"])  # NVDA (in the ledger) is not in this universe

        result = await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=True)

        assert not result.is_valid
        assert "NVDA" in result.unmapped_tickers


class TestBindingIsLogged:
    @pytest.mark.asyncio
    async def test_mapped_sample_document_is_logged(self, caplog):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with caplog.at_level(logging.INFO, logger="auspex.bootstrap"):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=True)

        assert any("mapped sample document" in record.message for record in caplog.records)
        assert any("txn-1" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_binding_summary_is_logged(self, caplog):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with caplog.at_level(logging.INFO, logger="auspex.bootstrap"):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=True)

        summary_lines = [r.message for r in caplog.records if "binding summary" in r.message]
        assert summary_lines
        assert "lot_level=True" in summary_lines[0]
        assert "holdings=1" in summary_lines[0]

    @pytest.mark.asyncio
    async def test_binding_is_logged_even_when_not_confirmed(self, caplog):
        """The whole point of the gate is that the operator reviews the log
        *before* setting the flag — logging must happen before the raise."""

        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with caplog.at_level(logging.INFO, logger="auspex.bootstrap"):
            with pytest.raises(PortfolioBindingNotConfirmedError):
                await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=False)

        assert any("mapped sample document" in record.message for record in caplog.records)
        assert any("binding summary" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_unconfirmed_error_message_names_the_env_var(self):
        adapter = PortfolioAdapter(FakeDatabase({"portfolio_transactions": [txn()]}), make_mapping())
        runner = make_runner(["NVDA"])

        with pytest.raises(PortfolioBindingNotConfirmedError, match="AUSPEX_CONFIRM_PORTFOLIO_BINDING"):
            await runner.bind_and_validate_portfolio(adapter, date(2026, 8, 8), confirmed=False)


class TestSettingsDefault:
    def test_confirm_portfolio_binding_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("AUSPEX_CONFIRM_PORTFOLIO_BINDING", raising=False)
        assert Settings().confirm_portfolio_binding is False

    def test_env_var_explicitly_enables_it(self, monkeypatch):
        monkeypatch.setenv("AUSPEX_CONFIRM_PORTFOLIO_BINDING", "true")
        assert Settings().confirm_portfolio_binding is True
