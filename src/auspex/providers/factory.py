"""Default provider wiring (arc42 §3.1).

Resolves Key Vault secrets and constructs the default price/FX/news/filing
providers used by the CLI and bootstrap orchestrations. Alpha Vantage is the
default `PriceProvider` + `FxProvider`; Finnhub is the default `NewsProvider`;
EDGAR requires no API key. Swapping any
vendor requires no change outside :mod:`auspex.providers`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from auspex.providers.alpha_vantage import AlphaVantageProvider
from auspex.providers.edgar import EdgarClient
from auspex.providers.finnhub import FinnhubNewsProvider
from auspex.providers.secrets import SecretResolver
from auspex.settings import Settings

logger = logging.getLogger("auspex.providers.factory")


@dataclass(frozen=True)
class DefaultProviders:
    price_and_fx: AlphaVantageProvider | None
    news: FinnhubNewsProvider | None
    edgar: EdgarClient


async def build_default_providers(settings: Settings, secrets: SecretResolver) -> DefaultProviders:
    """Construct the default arc42 §3.1 provider set.

    A provider whose Key Vault secret cannot be resolved (e.g. running
    outside the deployed environment, or a rotated/missing secret) is
    returned as ``None`` rather than raising: the caller wires that into
    :class:`~auspex.pipeline.context.PipelineProviders`, whose absence marks
    the corresponding collector step SKIPPED — the same graceful-degradation
    behaviour as any other single-provider failure (arc42 §6.1).
    """

    edgar = EdgarClient(
        base_url=settings.edgar_base_url,
        www_base_url=settings.edgar_www_base_url,
        user_agent=settings.edgar_user_agent,
        rate_limit_per_second=settings.edgar_rate_limit_per_second,
    )

    price_and_fx: AlphaVantageProvider | None = None
    try:
        price_api_key = await secrets.get_secret(settings.price_api_key_secret)
        price_and_fx = AlphaVantageProvider(base_url=settings.alpha_vantage_base_url, api_key=price_api_key)
    except Exception:  # noqa: BLE001 - degrade to no price/FX provider, do not abort the run
        logger.warning("could not resolve price provider secret %r", settings.price_api_key_secret, exc_info=True)

    news: FinnhubNewsProvider | None = None
    try:
        news_api_key = await secrets.get_secret(settings.news_api_key_secret)
        news = FinnhubNewsProvider(
            base_url=settings.finnhub_base_url,
            api_key=news_api_key,
            rate_limit_per_second=settings.finnhub_rate_limit_per_second,
        )
    except Exception:  # noqa: BLE001 - degrade to no news provider, do not abort the run
        logger.warning("could not resolve news provider secret %r", settings.news_api_key_secret, exc_info=True)

    return DefaultProviders(price_and_fx=price_and_fx, news=news, edgar=edgar)
