"""Cosmos read adapter for the live portfolio ledger (arc42 §5.7).

The source of truth is `portfolio_transactions`: an append-only, event-sourced
container (see :mod:`auspex.portfolio.event_ledger` for the derivation logic
This module reads that container and is typed against a narrow
:class:`ReadOnlyContainer` protocol that has no write method to call even by
mistake. Writes are isolated in :mod:`auspex.portfolio.ledger_service`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from auspex.portfolio.event_ledger import (
    LedgerTransaction,
    derive_cash_chf,
    derive_holdings,
    effective_transactions,
    summarize_ledger_financials,
)
from auspex.portfolio.mapping import PortfolioMappingConfig
from auspex.portfolio.port import PortfolioSnapshot


class ReadOnlyContainer(Protocol):
    """Exactly the read surface :class:`PortfolioAdapter` needs — no
    ``upsert_item``/``create_item``/``delete_item``/``replace_item`` in
    sight, so a zero-write guarantee can be checked structurally (a fake
    implementing only this protocol cannot possibly accept a write call)."""

    def query_items(
        self, query: str, parameters: list[dict[str, Any]] | None = None, partition_key: str | None = None
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]: ...


class ReadOnlyDatabase(Protocol):
    """Resolves the two containers the adapter reads from — again with no
    write methods on the surface."""

    def get_container_client(self, name: str) -> ReadOnlyContainer: ...


class OwnerResolutionError(RuntimeError):
    """Raised when neither a static ``owner_user_sk`` nor a working
    ``identity_mapping`` resolves to a partition value."""


class PortfolioAdapter:
    """Binds :class:`~auspex.portfolio.port.PortfolioPort` to the live,
    event-sourced `portfolio_transactions` container via `config/portfolio_mapping.yaml`.

    Every Cosmos call this class makes is a query or a point read —
    :class:`ReadOnlyContainer` has no write method to call even by mistake
    The API's separate write service owns mutation and validation.

    **Multi-user.** An adapter is bound to exactly one ledger partition. In a
    request path that partition comes from the authenticated caller
    (``owner_user_sk=...``); in the nightly fan-out it comes from the user
    currently being processed. Only legacy single-owner flows (bootstrap of a
    pre-existing imported ledger) leave it unset and fall back to resolving
    "the one active owner" from configuration. Because the binding is
    per-instance and immutable, an adapter can never be reused across users
    and silently serve the wrong partition — construct one per user.
    """

    def __init__(
        self,
        database: ReadOnlyDatabase,
        mapping: PortfolioMappingConfig,
        fx_rate_to_chf: Any | None = None,
        owner_user_sk: str | None = None,
    ) -> None:
        self._database = database
        self._mapping = mapping
        self._fx_rate_to_chf = fx_rate_to_chf
        self._owner_user_sk_cache: str | None = owner_user_sk or None
        self._explicit_owner_user_sk: str | None = owner_user_sk or None
        self._degraded_fields: set[str] = set()

    @property
    def owner_user_sk(self) -> str | None:
        """The explicitly bound ledger partition, if this adapter has one."""

        return self._explicit_owner_user_sk

    @property
    def holdings_container_name(self) -> str:
        return self._mapping.transactions.container

    def degraded_fields(self) -> list[str]:
        return sorted(self._degraded_fields)

    async def _resolve_owner_user_sk(self) -> str:
        if self._explicit_owner_user_sk is not None:
            return self._explicit_owner_user_sk
        if self._owner_user_sk_cache is not None:
            return self._owner_user_sk_cache
        if self._mapping.owner_user_sk:
            self._owner_user_sk_cache = self._mapping.owner_user_sk
            return self._owner_user_sk_cache

        identity = self._mapping.identity_mapping
        if identity is None:
            raise OwnerResolutionError(
                "config/portfolio_mapping.yaml has neither a static owner_user_sk "
                "nor an identity_mapping section configured"
            )
        container = self._database.get_container_client(identity.container)
        if not identity.identity_key:
            documents: list[dict[str, Any]] = []
            results = container.query_items(
                query="SELECT * FROM c WHERE c.status = @status",
                parameters=[{"name": "@status", "value": "active"}],
            )
            async for document in results:
                documents.append(document)
            if len(documents) != 1 or not documents[0].get("user_sk"):
                raise OwnerResolutionError(
                    "single-owner binding requires exactly one active app_users document"
                )
            self._owner_user_sk_cache = str(documents[0]["user_sk"])
            return self._owner_user_sk_cache
        try:
            document = await container.read_item(item=identity.identity_key, partition_key=identity.identity_key)
        except Exception as exc:  # noqa: BLE001 - surfaced as a clear binding failure, not a crash signature
            raise OwnerResolutionError(
                f"could not resolve owner_user_sk via app_users container "
                f"{identity.container!r} for identity_key {identity.identity_key!r}"
            ) from exc
        user_sk = document.get("user_sk")
        if not user_sk:
            raise OwnerResolutionError(
                f"app_users document for identity_key {identity.identity_key!r} has no user_sk field"
            )
        self._owner_user_sk_cache = str(user_sk)
        return self._owner_user_sk_cache

    async def resolve_owner_user_sk(self) -> str:
        return await self._resolve_owner_user_sk()

    async def _read_transaction_documents(self) -> list[dict[str, Any]]:
        owner_user_sk = await self._resolve_owner_user_sk()
        cfg = self._mapping.transactions
        container = self._database.get_container_client(cfg.container)
        query = f"SELECT * FROM c WHERE c.{cfg.partition_key_field} = @owner_user_sk AND c.id != @revision_id"
        parameters = [
            {"name": "@owner_user_sk", "value": owner_user_sk},
            {"name": "@revision_id", "value": cfg.revision_document_id},
        ]
        documents: list[dict[str, Any]] = []
        results = container.query_items(query=query, parameters=parameters, partition_key=owner_user_sk)
        async for document in results:
            documents.append(document)
        return documents

    async def read_snapshot(
        self,
        as_of: date,
        fx_rate_to_chf: Callable[[str], Decimal | None] | None = None,
    ) -> PortfolioSnapshot:
        """arc42 §5.7: replay the effective event ledger into current
        holdings and cash, with no persisted state of its own."""

        documents = await self._read_transaction_documents()
        transactions = [LedgerTransaction.from_document(d) for d in documents]
        effective = effective_transactions(transactions)
        holdings = derive_holdings(effective)
        resolver = fx_rate_to_chf or self._fx_rate_to_chf
        cash_chf = derive_cash_chf(effective, fx_rate_to_chf=resolver)
        summary = summarize_ledger_financials(
            effective,
            resolver or (lambda currency: Decimal(1) if currency == "CHF" else None),
        )
        self._degraded_fields = {
            field
            for field, missing in {
                "cost_basis_usd": any(holding.cost_basis_usd is None for holding in holdings),
                "cost_basis_chf": any(holding.cost_basis_chf is None for holding in holdings),
                "open_date": any(holding.open_date is None for holding in holdings),
                "lot_id": any(holding.lot_id is None for holding in holdings),
                "fx_rate_at_open": any(holding.fx_rate_at_open is None for holding in holdings),
            }.items()
            if missing
        }
        return PortfolioSnapshot(
            holdings=holdings,
            cash_chf=cash_chf,
            as_of=as_of,
            lot_level=True,
            dividends_chf=summary.dividends_chf,
            expenses_chf=summary.expenses_chf,
            withdrawals_chf=summary.withdrawals_chf,
            contributed_capital_chf=summary.contributed_capital_chf,
        )

    async def read_transactions(self) -> list[LedgerTransaction]:
        documents = await self._read_transaction_documents()
        return [LedgerTransaction.from_document(document) for document in documents]

    async def sample_holding_document(self) -> dict[str, Any] | None:
        """arc42 §6.3 step 11: one raw event document for owner confirmation
        that the binding is pointed at the right ledger before a run proceeds."""

        documents = await self._read_transaction_documents()
        return documents[0] if documents else None
