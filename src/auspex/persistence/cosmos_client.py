"""Cosmos DB client factory — managed identity, no connection strings (arc42 TC-04)."""

from __future__ import annotations

from functools import lru_cache

from azure.cosmos.aio import ContainerProxy, CosmosClient, DatabaseProxy
from azure.identity.aio import DefaultAzureCredential

from auspex.settings import Settings, get_settings

CONTAINER_PARTITION_KEYS: dict[str, str] = {
    "securities": "/security_id",
    "documents": "/security_id",
    "extractions": "/security_id",
    "digests": "/security_id",
    "narratives": "/cache_key",
    "market_daily": "/security_id",
    "fundamentals": "/security_id",
    "scores": "/security_id",
    "leg_changes": "/security_id",
    "recommendations": "/user_id",
    "portfolio_projection": "/user_id",
    "conversations": "/user_id",
    "performance": "/metric_type",
    "runs": "/run_date",
    "config_versions": "/config_type",
    "user_settings": "/user_id",
    "watermarks": "/scope",
}


class CosmosContext:
    """Holds the shared async Cosmos client/credential for process lifetime.

    Owns Auspex's *own* database (``cosmos-auspex``) — the container list
    above. The separate, owner-owned source portfolio ledger is a different
    Cosmos account entirely; see :class:`SourceLedgerCosmosContext`.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(self._settings.cosmos_account_endpoint, credential=self._credential)
        self._database: DatabaseProxy | None = None

    async def database(self) -> DatabaseProxy:
        if self._database is None:
            self._database = self._client.get_database_client(self._settings.cosmos_database_name)
        return self._database

    async def container(self, name: str) -> ContainerProxy:
        db = await self.database()
        return db.get_container_client(name)

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()


class SourceLedgerCosmosContext:
    """Binding to the owner-scoped live portfolio ledger account (arc42 §5.7).

    A distinct Cosmos account/database from Auspex's own (``AUSPEX_PORTFOLIO_COSMOS_ENDPOINT``
    / ``AUSPEX_PORTFOLIO_COSMOS_DATABASE``). RBAC on this account grants only the
    RBAC is assigned per workload in ``infra/modules/source-ledger-rbac.bicep``:
    pipeline/performance identities receive Data Reader, while the API receives
    container-scoped Data Contributor on ``portfolio_transactions`` and Data Reader
    on ``app_users``. Read derivation stays in :class:`PortfolioAdapter`; validated
    mutations stay in :class:`PortfolioLedgerService`.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(self._settings.portfolio_cosmos_endpoint, credential=self._credential)
        self._database: DatabaseProxy | None = None

    def get_container_client(self, name: str) -> ContainerProxy:
        if self._database is None:
            self._database = self._client.get_database_client(self._settings.portfolio_cosmos_database)
        return self._database.get_container_client(name)

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()


@lru_cache
def get_cosmos_context() -> CosmosContext:
    return CosmosContext()


@lru_cache
def get_source_ledger_context() -> SourceLedgerCosmosContext:
    return SourceLedgerCosmosContext()
