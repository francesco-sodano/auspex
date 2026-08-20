"""Strict per-user isolation of the portfolio ledger (arc42 §5.7, §11).

Before multi-user, a single ``PortfolioAdapter``/``PortfolioLedgerService``
was memoised for the whole process and asserted that the caller *was* the one
configured owner. That design cannot be made multi-tenant safe by adding a
check: a cached instance is bound to whoever constructed it first.

These tests pin the replacement contract:

* an adapter/service instance is bound to exactly one ledger partition;
* the partition is derived from the authenticated identity, never from the
  request;
* two users reading and writing concurrently see strictly their own events;
* the dependency providers construct a fresh binding per request.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from auspex.api import deps
from auspex.models.app_user import AppUserSummary, UserStatus
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.ledger_service import PortfolioLedgerService
from auspex.portfolio.mapping import PortfolioMappingConfig, TransactionsMappingConfig
from auspex.settings import Settings
from auspex.users.service import AppUserService

from .conftest import InMemoryAppUserRepository, make_app_user

ALICE = "user-alice"
BOB = "user-bob"


def ledger_document(owner: str, transaction_id: str, cash: str) -> dict:
    return {
        "id": transaction_id,
        "transaction_id": transaction_id,
        "owner_user_sk": owner,
        "transaction_type": "OPENING_CASH",
        "event_date": "2026-01-01",
        "currency": "CHF",
        "security_code": None,
        "quantity": None,
        "price": None,
        "cash_amount": cash,
        "cash_currency": "CHF",
        "fees": "0",
        "created_at": "2026-01-01T00:00:00Z",
    }


class SharedContainer:
    """One physical container holding several users' partitions."""

    def __init__(self, documents: list[dict]) -> None:
        self.documents = list(documents)
        self.queried_partitions: list[str | None] = []
        self.deleted: list[tuple[str, str]] = []

    def query_items(self, query: str, parameters=None, partition_key=None):
        self.queried_partitions.append(partition_key)
        rows = [
            document
            for document in self.documents
            if partition_key is None or document.get("owner_user_sk") == partition_key
        ]

        async def _iterate():
            if query.strip().upper().startswith("SELECT VALUE COUNT"):
                yield len(rows)
                return
            if query.strip().upper().startswith("SELECT VALUE C.ID"):
                for row in rows:
                    yield row["id"]
                return
            for row in rows:
                yield row

        return _iterate()

    async def read_item(self, item: str, partition_key: str) -> dict:
        for document in self.documents:
            if document["id"] == item and document.get("owner_user_sk") == partition_key:
                return document
        raise KeyError(item)

    async def create_item(self, body: dict) -> dict:
        self.documents.append(body)
        return body

    async def delete_item(self, item: str, partition_key: str) -> None:
        self.deleted.append((item, partition_key))
        self.documents = [
            document
            for document in self.documents
            if not (document["id"] == item and document.get("owner_user_sk") == partition_key)
        ]


class SharedDatabase:
    def __init__(self, container: SharedContainer) -> None:
        self.container = container

    def get_container_client(self, name: str) -> SharedContainer:
        return self.container


def mapping() -> PortfolioMappingConfig:
    return PortfolioMappingConfig(
        transactions=TransactionsMappingConfig(
            container="portfolio_transactions", partition_key_field="owner_user_sk"
        ),
        owner_user_sk=None,
        identity_mapping=None,
    )


def shared_ledger() -> SharedContainer:
    return SharedContainer(
        [
            ledger_document(ALICE, "alice-opening", "1000"),
            ledger_document(BOB, "bob-opening", "9999"),
        ]
    )


class TestAdapterBinding:
    @pytest.mark.asyncio
    async def test_adapter_reads_only_its_own_partition(self):
        container = shared_ledger()
        database = SharedDatabase(container)

        alice = PortfolioAdapter(database, mapping(), owner_user_sk=ALICE)
        bob = PortfolioAdapter(database, mapping(), owner_user_sk=BOB)

        alice_snapshot = await alice.read_snapshot(date(2026, 8, 20))
        bob_snapshot = await bob.read_snapshot(date(2026, 8, 20))

        assert alice_snapshot.cash_chf == Decimal("1000")
        assert bob_snapshot.cash_chf == Decimal("9999")
        assert set(container.queried_partitions) == {ALICE, BOB}

    @pytest.mark.asyncio
    async def test_an_explicitly_bound_adapter_never_resolves_a_global_owner(self):
        """No configuration lookup can re-point an already-bound adapter."""

        adapter = PortfolioAdapter(SharedDatabase(shared_ledger()), mapping(), owner_user_sk=ALICE)

        assert await adapter.resolve_owner_user_sk() == ALICE
        assert adapter.owner_user_sk == ALICE


class TestLedgerServiceBinding:
    def make_service(self, user_id: str, container: SharedContainer) -> PortfolioLedgerService:
        database = SharedDatabase(container)
        return PortfolioLedgerService(
            database,
            mapping(),
            PortfolioAdapter(database, mapping(), owner_user_sk=user_id),
            {"NVDA"},
            owner_user_sk=user_id,
            authenticated_user_id=user_id,
        )

    @pytest.mark.asyncio
    async def test_each_user_lists_only_their_own_transactions(self):
        container = shared_ledger()

        alice_rows = await self.make_service(ALICE, container).list_transactions(ALICE)
        bob_rows = await self.make_service(BOB, container).list_transactions(BOB)

        assert [row["transaction_id"] for row in alice_rows] == ["alice-opening"]
        assert [row["transaction_id"] for row in bob_rows] == ["bob-opening"]

    @pytest.mark.asyncio
    async def test_a_bound_service_refuses_a_different_authenticated_user(self):
        container = shared_ledger()
        service = self.make_service(ALICE, container)

        with pytest.raises(PermissionError):
            await service.list_transactions(BOB)

    @pytest.mark.asyncio
    async def test_an_empty_authenticated_user_is_refused(self):
        service = self.make_service(ALICE, shared_ledger())

        with pytest.raises(PermissionError):
            await service.list_transactions("")

    @pytest.mark.asyncio
    async def test_writes_land_in_the_callers_own_partition(self):
        container = shared_ledger()
        service = self.make_service(ALICE, container)

        created = await service.create_transaction(
            ALICE,
            {
                "client_request_id": "req-1",
                "transaction_type": "DEPOSIT",
                "event_date": date(2026, 2, 1),
                "currency": "CHF",
                "amount": "100",
                "fx_rate_to_base": "1",
                "fees": "0",
            },
        )

        assert created["owner_user_sk"] == ALICE
        bob_rows = await self.make_service(BOB, container).list_transactions(BOB)
        assert all(row["owner_user_sk"] == BOB for row in bob_rows)

    @pytest.mark.asyncio
    async def test_purge_only_removes_the_bound_users_events(self):
        container = shared_ledger()

        removed = await self.make_service(ALICE, container).purge_owner_ledger(ALICE)

        assert removed == 1
        assert await self.make_service(ALICE, container).count_owner_ledger(ALICE) == 0
        assert await self.make_service(BOB, container).count_owner_ledger(BOB) == 1


class TestDependencyProviders:
    def test_ledger_bindings_are_not_process_cached(self):
        """A memoised binding would serve the first caller's ledger to all."""

        assert not hasattr(deps.get_portfolio_adapter, "cache_info")
        assert not hasattr(deps.get_portfolio_ledger_service, "cache_info")

    def test_ledger_bindings_depend_on_the_authenticated_user(self):
        for provider in (deps.get_portfolio_adapter, deps.get_portfolio_ledger_service):
            parameters = inspect.signature(provider).parameters
            assert "user" in parameters, provider.__name__
            assert "service" in parameters, provider.__name__

    def test_builders_bind_the_partition_they_are_given(self, monkeypatch):
        container = shared_ledger()
        monkeypatch.setattr(deps, "get_source_ledger_context", lambda: SharedDatabase(container))
        monkeypatch.setattr(deps, "load_portfolio_mapping", mapping)

        alice = deps.build_portfolio_ledger_service(ALICE, ALICE)
        bob = deps.build_portfolio_ledger_service(BOB, BOB)

        assert alice is not bob
        assert alice._owner_user_sk == ALICE  # noqa: SLF001 - binding is the property under test
        assert bob._owner_user_sk == BOB  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_partition_resolution_uses_the_users_own_record(self):
        alice = make_app_user(ALICE, status=UserStatus.ACTIVE)
        legacy = make_app_user(BOB, status=UserStatus.ACTIVE, ledger_partition_key="legacy-owner-sk")
        service = AppUserService(
            user_repo=InMemoryAppUserRepository([alice, legacy]),
            index_repo=InMemoryAppUserRepository(
                [AppUserSummary.from_user(alice), AppUserSummary.from_user(legacy)]
            ),
            settings=Settings(initial_admin_email=""),
        )

        assert await deps.resolve_ledger_partition_key(ALICE, service) == ALICE
        # A pre-existing imported ledger keeps its historical partition.
        assert await deps.resolve_ledger_partition_key(BOB, service) == "legacy-owner-sk"

    @pytest.mark.asyncio
    async def test_unregistered_principal_falls_back_to_their_own_id(self):
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email=""),
        )

        assert await deps.resolve_ledger_partition_key("user-nobody", service) == "user-nobody"


class TestLegacyOwnerPreservation:
    @pytest.mark.asyncio
    async def test_configured_owner_keeps_a_pinned_legacy_partition(self, monkeypatch):
        """The production owner's existing ledger must remain readable.

        When ``portfolio_mapping.yaml`` pins a static ``owner_user_sk`` and the
        registering principal is the configured owner, that partition is
        carried onto their ``app_users`` record rather than being replaced by
        the derived ``user_id``.
        """

        monkeypatch.setattr(
            "auspex.users.service._configured_legacy_owner_partition", lambda: "legacy-owner-sk"
        )
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            settings=Settings(initial_admin_email="", owner_provider_user_id="oid-owner"),
        )

        owner = await service.register(provider_user_id="oid-owner")
        newcomer = await service.register(provider_user_id="oid-newcomer")

        assert owner.ledger_partition_key == "legacy-owner-sk"
        assert newcomer.ledger_partition_key == newcomer.user_id

    @pytest.mark.asyncio
    async def test_explicit_partition_override_wins(self, monkeypatch):
        """The safety valve for a ledger that was resolved dynamically.

        Where the legacy partition cannot be read from configuration at all,
        an operator can pin it explicitly rather than silently stranding the
        owner's history under a new partition.
        """

        monkeypatch.setattr("auspex.users.service._configured_legacy_owner_partition", lambda: None)
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            settings=Settings(
                initial_admin_email="",
                owner_provider_user_id="oid-owner",
                owner_ledger_partition_key="pinned-owner-sk",
            ),
        )

        owner = await service.register(provider_user_id="oid-owner")

        assert owner.ledger_partition_key == "pinned-owner-sk"

    @pytest.mark.asyncio
    async def test_override_does_not_apply_to_other_users(self, monkeypatch):
        monkeypatch.setattr("auspex.users.service._configured_legacy_owner_partition", lambda: None)
        service = AppUserService(
            user_repo=InMemoryAppUserRepository(),
            index_repo=InMemoryAppUserRepository(),
            settings=Settings(
                initial_admin_email="",
                owner_provider_user_id="oid-owner",
                owner_ledger_partition_key="pinned-owner-sk",
            ),
        )

        other = await service.register(provider_user_id="oid-someone-else")

        assert other.ledger_partition_key == other.user_id
