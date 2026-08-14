"""FastAPI dependency providers — Cosmos/Blob repositories via managed identity."""

from __future__ import annotations

from functools import lru_cache

from auspex.config.loader import Universe, load_universe
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.performance import PerformanceMetric
from auspex.models.policy import Recommendation
from auspex.models.portfolio import PortfolioProjection
from auspex.models.run import RunManifest
from auspex.models.scoring import ScoreSnapshot
from auspex.models.user_settings import UserSettings
from auspex.persistence.cosmos_client import CosmosContext, get_cosmos_context, get_source_ledger_context
from auspex.persistence.repositories import CosmosFxSink, CosmosPriceSink, CosmosRepository
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.ledger_service import PortfolioLedgerService
from auspex.portfolio.mapping import load_portfolio_mapping


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
def get_portfolio_projection_repo() -> CosmosRepository[PortfolioProjection]:
    """Auspex's own, freely-rewritten daily projection (arc42 §5.7) — the only
    portfolio-related container Auspex writes to."""

    return CosmosRepository(get_cosmos_context(), "portfolio_projection", PortfolioProjection)


@lru_cache
def get_performance_repo() -> CosmosRepository[PerformanceMetric]:
    return CosmosRepository(get_cosmos_context(), "performance", PerformanceMetric)


@lru_cache
def get_run_repo() -> CosmosRepository[RunManifest]:
    return CosmosRepository(get_cosmos_context(), "runs", RunManifest)


@lru_cache
def get_user_settings_repo() -> CosmosRepository[UserSettings]:
    return CosmosRepository(get_cosmos_context(), "user_settings", UserSettings)


@lru_cache
def get_price_sink() -> CosmosPriceSink:
    return CosmosPriceSink(get_cosmos_context())


@lru_cache
def get_fx_sink() -> CosmosFxSink:
    return CosmosFxSink(get_cosmos_context())


@lru_cache
def get_portfolio_adapter() -> PortfolioAdapter:
    return PortfolioAdapter(get_source_ledger_context(), load_portfolio_mapping())


@lru_cache
def get_portfolio_ledger_service() -> PortfolioLedgerService:
    universe = load_universe()
    adapter = get_portfolio_adapter()
    return PortfolioLedgerService(
        get_source_ledger_context(),
        load_portfolio_mapping(),
        adapter,
        set(universe.by_ticker()),
    )
