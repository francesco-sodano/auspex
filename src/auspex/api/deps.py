"""FastAPI dependency providers — Cosmos/Blob repositories via managed identity.

Two kinds of provider live here:

*Container repositories* are process-wide and safely memoised: a
:class:`~auspex.persistence.repositories.CosmosRepository` is a stateless
wrapper that takes the partition key on every call, so sharing one across
users cannot leak anything.

*Ledger bindings* are emphatically **not** memoised. A
:class:`~auspex.portfolio.adapter.PortfolioAdapter` and a
:class:`~auspex.portfolio.ledger_service.PortfolioLedgerService` are each
bound to one ledger partition for their whole lifetime, so a cached instance
would serve the first caller's ledger to everybody afterwards. They are
constructed per request from the authenticated principal instead
(:func:`get_portfolio_adapter`, :func:`get_portfolio_ledger_service`), and
the partition is derived from the caller's own ``app_users`` record — never
from anything in the request.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.config.loader import Universe, load_universe
from auspex.models.app_user import AdminAuthorityBinding, AppUser, AppUserSummary
from auspex.models.audit import UserAuditEvent
from auspex.models.deletion import DeletionJob
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.onboarding import OnboardingState
from auspex.models.performance import PerformanceMetric
from auspex.models.policy import Recommendation, RecommendationDisposition
from auspex.models.portfolio import PortfolioProjection
from auspex.models.run import RunManifest
from auspex.models.scoring import ScoreSnapshot
from auspex.models.user_settings import UserSettings
from auspex.persistence.cosmos_client import CosmosContext, get_cosmos_context, get_source_ledger_context
from auspex.persistence.repositories import CosmosFxSink, CosmosPriceSink, CosmosRepository
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.ledger_service import PortfolioLedgerService
from auspex.portfolio.mapping import load_portfolio_mapping
from auspex.users.service import AppUserService


def get_universe() -> Universe:
    return load_universe()


def get_context() -> CosmosContext:
    return get_cosmos_context()


@lru_cache
def get_score_repo() -> CosmosRepository[ScoreSnapshot]:
    return CosmosRepository(get_cosmos_context(), "scores", ScoreSnapshot)


@lru_cache
def get_fundamental_repo() -> CosmosRepository[FundamentalSnapshot]:
    return CosmosRepository(get_cosmos_context(), "fundamentals", FundamentalSnapshot)


@lru_cache
def get_recommendation_repo() -> CosmosRepository[Recommendation]:
    return CosmosRepository(get_cosmos_context(), "recommendations", Recommendation)


@lru_cache
def get_recommendation_disposition_repo() -> CosmosRepository[RecommendationDisposition]:
    """Durable per-(user, security) dispositions that suppress re-asking."""

    return CosmosRepository(get_cosmos_context(), "recommendation_dispositions", RecommendationDisposition)


@lru_cache
def get_portfolio_projection_repo() -> CosmosRepository[PortfolioProjection]:
    """Auspex's own, freely-rewritten daily projection (arc42 §5.7) — the only
    portfolio-related container Auspex writes to."""

    return CosmosRepository(get_cosmos_context(), "portfolio_projection", PortfolioProjection)


@lru_cache
def get_performance_repo() -> CosmosRepository[PerformanceMetric]:
    return CosmosRepository(get_cosmos_context(), "performance", PerformanceMetric)


@lru_cache
def get_user_performance_repo() -> CosmosRepository[PerformanceMetric]:
    return CosmosRepository(get_cosmos_context(), "user_performance", PerformanceMetric)


@lru_cache
def get_run_repo() -> CosmosRepository[RunManifest]:
    return CosmosRepository(get_cosmos_context(), "runs", RunManifest)


@lru_cache
def get_user_settings_repo() -> CosmosRepository[UserSettings]:
    return CosmosRepository(get_cosmos_context(), "user_settings", UserSettings)


@lru_cache
def get_app_user_repo() -> CosmosRepository[AppUser]:
    return CosmosRepository(get_cosmos_context(), "app_users", AppUser)


@lru_cache
def get_app_user_index_repo() -> CosmosRepository[AppUserSummary]:
    return CosmosRepository(get_cosmos_context(), "app_user_index", AppUserSummary)


@lru_cache
def get_admin_binding_repo() -> CosmosRepository[AdminAuthorityBinding]:
    """Same container as the roster, read through the binding model."""

    return CosmosRepository(get_cosmos_context(), "app_user_index", AdminAuthorityBinding)


@lru_cache
def get_audit_repo() -> CosmosRepository[UserAuditEvent]:
    return CosmosRepository(get_cosmos_context(), "audit_events", UserAuditEvent)


@lru_cache
def get_onboarding_repo() -> CosmosRepository[OnboardingState]:
    return CosmosRepository(get_cosmos_context(), "onboarding", OnboardingState)


@lru_cache
def get_deletion_job_repo() -> CosmosRepository[DeletionJob]:
    return CosmosRepository(get_cosmos_context(), "deletion_jobs", DeletionJob)


@lru_cache
def get_price_sink() -> CosmosPriceSink:
    return CosmosPriceSink(get_cosmos_context())


@lru_cache
def get_fx_sink() -> CosmosFxSink:
    return CosmosFxSink(get_cosmos_context())


class IndexRepositoryFacade:
    """Reads the roster container through whichever model a row declares.

    ``app_user_index`` holds two shapes in one partition: user summaries and
    the singleton admin binding. Cosmos does not care, but the typed
    repository does, so this facade routes by id/type and keeps
    :class:`~auspex.users.service.AppUserService` free of that detail.
    """

    def __init__(
        self,
        summary_repo: CosmosRepository[AppUserSummary],
        binding_repo: CosmosRepository[AdminAuthorityBinding],
    ) -> None:
        self._summaries = summary_repo
        self._bindings = binding_repo

    async def get(self, id_: str, partition_key: str):
        from auspex.models.app_user import ADMIN_BINDING_ID

        if id_ == ADMIN_BINDING_ID:
            return await self._bindings.get(id_, partition_key)
        return await self._summaries.get(id_, partition_key)

    async def upsert(self, item) -> None:
        if isinstance(item, AdminAuthorityBinding):
            await self._bindings.upsert(item)
            return
        await self._summaries.upsert(item)

    async def delete(self, id_: str, partition_key: str) -> bool:
        from auspex.models.app_user import ADMIN_BINDING_ID

        if id_ == ADMIN_BINDING_ID:
            return await self._bindings.delete(id_, partition_key)
        return await self._summaries.delete(id_, partition_key)

    async def query(self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None):
        return await self._summaries.query(query, parameters, partition_key)

    async def get_with_etag(self, id_: str, partition_key: str):
        from auspex.models.app_user import ADMIN_BINDING_ID

        if id_ == ADMIN_BINDING_ID:
            return await self._bindings.get_with_etag(id_, partition_key)
        return await self._summaries.get_with_etag(id_, partition_key)

    async def replace_if_match(self, item, etag: str) -> bool:
        if isinstance(item, AdminAuthorityBinding):
            return await self._bindings.replace_if_match(item, etag)
        return await self._summaries.replace_if_match(item, etag)


@lru_cache
def get_app_user_index() -> IndexRepositoryFacade:
    return IndexRepositoryFacade(get_app_user_index_repo(), get_admin_binding_repo())


def get_app_user_service() -> AppUserService:
    """Stateless service over the user containers — safe to build per request."""

    return AppUserService(
        user_repo=get_app_user_repo(),
        index_repo=get_app_user_index(),
        audit_repo=get_audit_repo(),
    )


def build_portfolio_adapter(ledger_partition_key: str) -> PortfolioAdapter:
    """A read adapter bound to exactly one ledger partition."""

    return PortfolioAdapter(
        get_source_ledger_context(),
        load_portfolio_mapping(),
        owner_user_sk=ledger_partition_key,
    )


def build_portfolio_ledger_service(
    user_id: str, ledger_partition_key: str | None = None
) -> PortfolioLedgerService:
    """A write service bound to exactly one authenticated user."""

    partition = ledger_partition_key or user_id
    universe = load_universe()
    return PortfolioLedgerService(
        get_source_ledger_context(),
        load_portfolio_mapping(),
        build_portfolio_adapter(partition),
        set(universe.by_ticker()),
        owner_user_sk=partition,
        authenticated_user_id=user_id,
    )


async def resolve_ledger_partition_key(user_id: str, service: AppUserService) -> str:
    app_user = await service.get_user(user_id)
    return app_user.ledger_partition_key if app_user is not None else user_id


def get_ledger_service_builder():
    """Factory for a ledger binding for an *arbitrary* user.

    Ordinary routes never need this — they get a binding for the caller from
    :func:`get_portfolio_ledger_service`. The administrator deletion path does:
    it must address the *subject's* ledger partition, not the acting
    administrator's. Exposing it as a dependency (rather than importing the
    builder directly) keeps that path substitutable in tests without reaching
    for a live Cosmos account.
    """

    return build_portfolio_ledger_service


async def get_portfolio_adapter(
    user: AuthenticatedUser = Depends(get_current_user),
    service: AppUserService = Depends(get_app_user_service),
) -> PortfolioAdapter:
    """Per-request ledger read binding for the authenticated caller."""

    return build_portfolio_adapter(await resolve_ledger_partition_key(user.user_id, service))


async def get_portfolio_ledger_service(
    user: AuthenticatedUser = Depends(get_current_user),
    service: AppUserService = Depends(get_app_user_service),
) -> PortfolioLedgerService:
    """Per-request ledger write binding for the authenticated caller."""

    partition = await resolve_ledger_partition_key(user.user_id, service)
    return build_portfolio_ledger_service(user.user_id, partition)
