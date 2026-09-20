"""Refresh provider-calculated current fundamentals without running the engine."""

from __future__ import annotations

import logging

from auspex.collectors.company_overview_collector import refresh_company_overviews
from auspex.config import load_universe
from auspex.models.company_overview import CompanyOverviewSnapshot
from auspex.persistence.cosmos_client import get_cosmos_context
from auspex.persistence.repositories import CosmosRepository
from auspex.providers.alpha_vantage import AlphaVantageProvider
from auspex.providers.secrets import get_secret_resolver
from auspex.settings import get_settings

logger = logging.getLogger(__name__)


async def refresh_company_overviews_command(tickers: list[str], *, force: bool = False) -> int:
    universe = load_universe()
    requested = {ticker.strip().upper() for ticker in tickers}
    unknown = requested - set(universe.by_ticker())
    if unknown:
        logger.error("Unknown overview tickers: %s", ", ".join(sorted(unknown)))
        return 1
    securities = [security for security in universe.securities if not requested or security.ticker in requested]
    settings = get_settings()
    context = get_cosmos_context()
    secrets = get_secret_resolver(settings.key_vault_url)
    provider = None
    try:
        provider = AlphaVantageProvider(
            base_url=settings.alpha_vantage_base_url,
            api_key=await secrets.get_secret(settings.price_api_key_secret),
        )
        result = await refresh_company_overviews(
            securities, provider, CosmosRepository(context, "company_overviews", CompanyOverviewSnapshot), force=force,
        )
        logger.info(
            "Company overviews complete: refreshed=%d cached=%d failed=%s",
            result.refreshed, result.cached, ",".join(result.failed_tickers) or "none",
        )
        return 1 if result.failed_tickers else 0
    finally:
        if provider is not None:
            await provider.aclose()
        await secrets.aclose()
        await context.aclose()
