"""Provider abstractions and default vendor implementations (arc42 §3.1).

``PriceProvider``, ``NewsProvider``, and ``FxProvider`` are interfaces;
swapping a vendor requires no change outside this package.
"""

from __future__ import annotations

from auspex.providers.alpha_vantage import AlphaVantageProvider
from auspex.providers.base import (
    FxProvider,
    FxRateDTO,
    NewsArticleDTO,
    NewsProvider,
    PriceBarDTO,
    PriceProvider,
)
from auspex.providers.edgar import EdgarClient
from auspex.providers.factory import DefaultProviders, build_default_providers
from auspex.providers.finnhub import FinnhubNewsProvider
from auspex.providers.fx_provider import ExchangeRateFxProvider, business_days_between
from auspex.providers.openai_provider import AzureOpenAIClient
from auspex.providers.rate_limit import TokenBucket, backoff_sleep
from auspex.providers.secrets import SecretResolver, get_secret_resolver
from auspex.providers.tiingo import TiingoPriceProvider

__all__ = [
    "AlphaVantageProvider",
    "FxProvider",
    "FxRateDTO",
    "NewsArticleDTO",
    "NewsProvider",
    "PriceBarDTO",
    "PriceProvider",
    "EdgarClient",
    "DefaultProviders",
    "build_default_providers",
    "FinnhubNewsProvider",
    "ExchangeRateFxProvider",
    "business_days_between",
    "AzureOpenAIClient",
    "TokenBucket",
    "backoff_sleep",
    "SecretResolver",
    "get_secret_resolver",
    "TiingoPriceProvider",
]
