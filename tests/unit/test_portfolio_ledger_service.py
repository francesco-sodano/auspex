from __future__ import annotations

from datetime import date

import pytest

from auspex.portfolio.ledger_service import PortfolioLedgerService, PortfolioLedgerValidationError
from auspex.portfolio.mapping import PortfolioMappingConfig, TransactionsMappingConfig


class Container:
    def __init__(self, documents: list[dict] | None = None) -> None:
        self.documents = {document["id"]: document for document in (documents or [])}

    def query_items(self, query, parameters=None, partition_key=None):
        owner = next(
            (parameter["value"] for parameter in parameters or [] if parameter["name"] == "@owner"),
            partition_key,
        )

        async def rows():
            for document in self.documents.values():
                if document.get("owner_user_sk") == owner and document["id"] != "_ledger_revision":
                    yield document

        return rows()

    async def read_item(self, item, partition_key):
        document = self.documents[item]
        if document.get("owner_user_sk") != partition_key:
            raise KeyError(item)
        return document

    async def create_item(self, body):
        self.documents[body["id"]] = body
        return body


class Database:
    def __init__(self, container: Container) -> None:
        self.container = container

    def get_container_client(self, name):
        return self.container


class Adapter:
    async def resolve_owner_user_sk(self):
        return "owner-1"


def transaction(**overrides):
    row = {
        "id": "opening-cash",
        "transaction_id": "opening-cash",
        "owner_user_sk": "owner-1",
        "transaction_type": "OPENING_CASH",
        "event_date": "2026-01-01",
        "currency": "CHF",
        "security_code": None,
        "quantity": None,
        "price": None,
        "cash_amount": "10000",
        "cash_currency": "CHF",
        "fees": "0",
        "created_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def service(documents=None):
    container = Container(documents)
    mapping = PortfolioMappingConfig(
        transactions=TransactionsMappingConfig(
            container="portfolio_transactions",
            partition_key_field="owner_user_sk",
        ),
        owner_user_sk="owner-1",
        identity_mapping=None,
    )
    return PortfolioLedgerService(
        Database(container),
        mapping,
        Adapter(),
        {"NVDA", "AMD"},
    ), container


@pytest.mark.asyncio
async def test_creates_normalized_buy_with_cash_effect() -> None:
    ledger, container = service(
        [
            transaction(),
            transaction(
                id="opening-chf",
                transaction_id="opening-chf",
                currency="CHF",
                cash_amount="100",
            ),
        ]
    )

    created = await ledger.create_transaction(
        "owner-1",
        {
            "client_request_id": "request-1",
            "transaction_type": "BUY",
            "event_date": date(2026, 2, 1),
            "currency": "USD",
            "security_code": "nvda",
            "quantity": "10",
            "price": "100",
            "fees": "0",
            "cost_components": [
                {"category": "BROKER_COMMISSION", "amount": "5", "currency": "USD"},
                {"category": "TRANSACTION_TAX", "amount": "2", "currency": "CHF"},
                {"category": "OTHER_FEE", "amount": "3", "currency": "USD"},
            ],
            "fx_rate_to_base": "0.8",
        },
    )

    assert created["security_code"] == "NVDA"
    assert created["cash_amount"] == "-800.0"
    assert created["cash_currency"] == "CHF"
    assert created["gross_amount"] == "1000"
    assert [item["category"] for item in created["cost_components"]] == [
        "BROKER_COMMISSION",
        "TRANSACTION_TAX",
        "OTHER_FEE",
    ]
    assert [item["currency"] for item in created["cost_components"]] == ["USD", "CHF", "USD"]
    assert created["cost_components_affect_cash"] is True
    assert created["id"] in container.documents


@pytest.mark.asyncio
async def test_rejects_sell_above_held_quantity() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="buy-1",
                transaction_id="buy-1",
                transaction_type="BUY",
                security_code="NVDA",
                quantity="2",
                price="100",
                cash_amount="-200",
            ),
        ]
    )

    with pytest.raises(PortfolioLedgerValidationError, match="only 2 is held"):
        await ledger.create_transaction(
            "owner-1",
            {
                "client_request_id": "request-2",
                "transaction_type": "SELL",
                "event_date": "2026-02-01",
                "currency": "USD",
                "security_code": "NVDA",
                "quantity": "3",
                "price": "120",
                "fees": "0",
                "fx_rate_to_base": "0.8",
            },
        )


@pytest.mark.asyncio
async def test_rejects_buy_above_same_currency_cash() -> None:
    ledger, _ = service([transaction(cash_amount="100")])

    with pytest.raises(PortfolioLedgerValidationError, match="insufficient CHF cash"):
        await ledger.create_transaction(
            "owner-1",
            {
                "client_request_id": "request-too-large",
                "transaction_type": "BUY",
                "event_date": "2026-02-01",
                "currency": "USD",
                "security_code": "NVDA",
                "quantity": "2",
                "price": "100",
                "fees": "0",
                "fx_rate_to_base": "0.8",
            },
        )


@pytest.mark.asyncio
async def test_dividend_keeps_withholding_as_currency_specific_child_cost() -> None:
    ledger, _ = service([transaction()])

    created = await ledger.create_transaction(
        "owner-1",
        {
            "client_request_id": "dividend-1",
            "transaction_type": "DIVIDEND",
            "event_date": "2026-02-01",
            "currency": "USD",
            "security_code": "NVDA",
            "amount": "100",
            "taxes": "15",
            "fees": "0",
            "fx_rate_to_base": "0.8",
        },
    )

    assert created["gross_amount"] == "100"
    assert created["cash_amount"] == "80.0"
    assert created["cash_currency"] == "CHF"
    assert created["cost_components"] == [
        {"category": "WITHHOLDING_TAX", "amount": "15", "currency": "USD"}
    ]


@pytest.mark.asyncio
async def test_edit_appends_correction_and_delete_appends_void() -> None:
    ledger, container = service(
        [
            transaction(),
            transaction(
                id="buy-1",
                transaction_id="buy-1",
                transaction_type="BUY",
                security_code="NVDA",
                quantity="2",
                price="100",
                cash_amount="-200",
            ),
        ]
    )

    corrected = await ledger.correct_transaction(
        "owner-1",
        "buy-1",
        {
            "client_request_id": "edit-1",
            "transaction_type": "BUY",
            "event_date": "2026-02-02",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "3",
            "price": "90",
            "fees": "0",
            "fx_rate_to_base": "0.8",
        },
    )
    assert corrected["corrects_transaction_id"] == "buy-1"

    voided = await ledger.void_transaction("owner-1", corrected["id"], "delete-1")
    assert voided["transaction_type"] == "VOID"
    assert voided["corrects_transaction_id"] == corrected["id"]
    assert len(container.documents) == 4


@pytest.mark.asyncio
async def test_correction_validates_against_state_without_original_event() -> None:
    ledger, _ = service(
        [
            transaction(cash_amount="300"),
            transaction(
                id="buy-1",
                transaction_id="buy-1",
                transaction_type="BUY",
                security_code="NVDA",
                quantity="3",
                price="100",
                cash_amount="-300",
            ),
        ]
    )

    corrected = await ledger.correct_transaction(
        "owner-1",
        "buy-1",
        {
            "client_request_id": "edit-boundary",
            "transaction_type": "BUY",
            "event_date": "2026-02-02",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "2",
            "price": "100",
            "fees": "0",
            "fx_rate_to_base": "0.8",
        },
    )
    assert corrected["cash_amount"] == "-160.0"


@pytest.mark.asyncio
async def test_list_returns_only_latest_effective_rows() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(id="buy-1", transaction_id="buy-1", transaction_type="BUY"),
            transaction(
                id="buy-2",
                transaction_id="buy-2",
                transaction_type="BUY",
                corrects_transaction_id="buy-1",
            ),
            transaction(
                id="void-1",
                transaction_id="void-1",
                transaction_type="VOID",
                corrects_transaction_id="buy-2",
                affects_cash=False,
            ),
        ]
    )

    rows = await ledger.list_transactions("owner-1")
    assert {row["transaction_id"] for row in rows} == {"opening-cash"}


@pytest.mark.asyncio
async def test_list_folds_linked_fee_rows_under_parent() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="position-1",
                transaction_id="position-1",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="10",
                price="100",
                cash_amount="0",
            ),
            transaction(
                id="commission-1",
                transaction_id="commission-1",
                transaction_type="FEE",
                linked_transaction_id="position-1",
                cash_amount="-12",
                currency="CHF",
                cost_category="BROKER_COMMISSION",
            ),
        ]
    )

    rows = await ledger.list_transactions("owner-1")

    assert {row["transaction_id"] for row in rows} == {
        "opening-cash",
        "position-1",
    }
    position = next(row for row in rows if row["transaction_id"] == "position-1")
    assert position["cost_components"] == [
        {
            "category": "BROKER_COMMISSION",
            "amount": "12",
            "currency": "CHF",
            "source_amount": None,
            "source_currency": None,
            "fx_rate_to_settlement": None,
        }
    ]


@pytest.mark.asyncio
async def test_list_uses_recorded_source_amount_for_zero_cash_opening_cost() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="position-1",
                transaction_id="position-1",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="10",
                price="100",
                cash_amount="0",
                fx_rate_to_base="0.8",
            ),
            transaction(
                id="commission-1",
                transaction_id="commission-1",
                transaction_type="FEE",
                linked_transaction_id="position-1",
                cash_amount="0",
                gross_amount="8",
                currency="CHF",
                source_amount="10",
                source_currency="USD",
                cost_category="BROKER_COMMISSION",
                affects_cash=False,
            ),
        ]
    )

    rows = await ledger.list_transactions("owner-1")
    position = next(row for row in rows if row["transaction_id"] == "position-1")

    assert position["cost_components"][0]["amount"] == "10"
    assert position["cost_components"][0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_correction_inherits_linked_costs_when_payload_omits_them() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="position-1",
                transaction_id="position-1",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="10",
                price="100",
                cash_amount="0",
                fx_rate_to_base="0.8",
            ),
            transaction(
                id="commission-1",
                transaction_id="commission-1",
                transaction_type="FEE",
                linked_transaction_id="position-1",
                cash_amount="0",
                gross_amount="8",
                currency="CHF",
                source_amount="10",
                source_currency="USD",
                cost_category="BROKER_COMMISSION",
                affects_cash=False,
            ),
        ]
    )

    corrected = await ledger.correct_transaction(
        "owner-1",
        "position-1",
        {
            "client_request_id": "edit-preserves-cost",
            "transaction_type": "OPENING_POSITION",
            "event_date": "2026-02-01",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "11",
            "price": "100",
            "fees": "0",
        },
    )

    assert corrected["fx_rate_to_base"] == "0.8"
    assert corrected["cost_components"] == [
        {"category": "BROKER_COMMISSION", "amount": "10", "currency": "USD"}
    ]


@pytest.mark.asyncio
async def test_list_restores_costs_lost_by_a_legacy_correction() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="position-1",
                transaction_id="position-1",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="10",
                price="100",
                cash_amount="0",
                fx_rate_to_base="0.8",
            ),
            transaction(
                id="commission-1",
                transaction_id="commission-1",
                transaction_type="FEE",
                linked_transaction_id="position-1",
                cash_amount="0",
                gross_amount="8",
                currency="CHF",
                source_amount="10",
                source_currency="USD",
                cost_category="BROKER_COMMISSION",
                affects_cash=False,
            ),
            transaction(
                id="position-2",
                transaction_id="position-2",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="11",
                price="100",
                cash_amount="0",
                fx_rate_to_base="0.8",
                corrects_transaction_id="position-1",
                created_at="2026-02-01T00:00:00Z",
            ),
        ]
    )

    rows = await ledger.list_transactions("owner-1")
    position = next(row for row in rows if row["transaction_id"] == "position-2")

    assert position["cost_components"][0]["amount"] == "10"
    assert position["cost_components"][0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_correction_can_explicitly_remove_all_costs() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="position-1",
                transaction_id="position-1",
                transaction_type="OPENING_POSITION",
                security_code="NVDA",
                quantity="10",
                price="100",
                cash_amount="0",
                fx_rate_to_base="0.8",
                cost_components=[
                    {"category": "OTHER_FEE", "amount": "4", "currency": "CHF"}
                ],
            ),
        ]
    )

    corrected = await ledger.correct_transaction(
        "owner-1",
        "position-1",
        {
            "client_request_id": "edit-removes-cost",
            "transaction_type": "OPENING_POSITION",
            "event_date": "2026-02-01",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "10",
            "price": "100",
            "fees": "0",
            "cost_components": [],
            "fx_rate_to_base": "0.8",
        },
    )

    assert corrected["cost_components"] == []


@pytest.mark.asyncio
async def test_transaction_can_link_to_a_followed_auspex_recommendation() -> None:
    ledger, _ = service([transaction()])

    created = await ledger.create_transaction(
        "owner-1",
        {
            "client_request_id": "followed-recommendation",
            "transaction_type": "BUY",
            "event_date": "2026-02-01",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "2",
            "price": "100",
            "fees": "0",
            "cost_components": [],
            "fx_rate_to_base": "0.8",
            "followed_auspex": True,
            "recommendation_id": "owner-1:sec-nvda:2026-02-01",
        },
    )

    assert created["followed_auspex"] is True
    assert created["recommendation_id"] == "owner-1:sec-nvda:2026-02-01"


@pytest.mark.asyncio
async def test_followed_suggestion_requires_recommendation_id() -> None:
    ledger, _ = service([transaction()])

    with pytest.raises(
        PortfolioLedgerValidationError,
        match="recommendation_id is required",
    ):
        await ledger.create_transaction(
            "owner-1",
            {
                "client_request_id": "missing-recommendation",
                "transaction_type": "BUY",
                "event_date": "2026-02-01",
                "currency": "USD",
                "security_code": "NVDA",
                "quantity": "2",
                "price": "100",
                "fees": "0",
                "cost_components": [],
                "fx_rate_to_base": "0.8",
                "followed_auspex": True,
            },
        )


@pytest.mark.asyncio
async def test_usd_cash_flow_correction_preserves_source_amount_without_double_fx() -> None:
    ledger, _ = service(
        [
            transaction(),
            transaction(
                id="dividend-1",
                transaction_id="dividend-1",
                transaction_type="DIVIDEND",
                security_code="NVDA",
                currency="USD",
                gross_amount="100",
                cash_amount="80",
                cash_currency="CHF",
                fx_rate_to_base="0.8",
            ),
        ]
    )

    corrected = await ledger.correct_transaction(
        "owner-1",
        "dividend-1",
        {
            "client_request_id": "correct-dividend",
            "transaction_type": "DIVIDEND",
            "event_date": "2026-02-02",
            "currency": "USD",
            "security_code": "NVDA",
            "amount": "100",
            "fees": "0",
            "cost_components": [],
            "fx_rate_to_base": "0.8",
        },
    )

    assert corrected["gross_amount"] == "100"
    assert corrected["cash_amount"] == "80.0"
    assert corrected["cash_currency"] == "CHF"


@pytest.mark.asyncio
async def test_legacy_usd_cash_is_reconciled_to_chf_for_sufficiency() -> None:
    legacy_cash = transaction(
        currency="USD",
        cash_amount="1000",
        fx_rate_to_base="0.8",
    )
    legacy_cash.pop("cash_currency")
    ledger, _ = service([legacy_cash])

    created = await ledger.create_transaction(
        "owner-1",
        {
            "client_request_id": "buy-from-legacy-cash",
            "transaction_type": "BUY",
            "event_date": "2026-02-02",
            "currency": "USD",
            "security_code": "NVDA",
            "quantity": "5",
            "price": "100",
            "fees": "0",
            "cost_components": [],
            "fx_rate_to_base": "0.8",
        },
    )

    assert created["cash_amount"] == "-400.0"
    assert created["cash_currency"] == "CHF"
