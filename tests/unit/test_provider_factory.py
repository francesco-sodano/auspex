"""Unit tests for default provider wiring (arc42 §3.1).

Alpha Vantage is the default price/FX provider and Finnhub the default news
provider. A provider whose secret cannot be resolved degrades to ``None``
rather than raising.
"""

from __future__ import annotations

import pytest

from auspex.providers.alpha_vantage import AlphaVantageProvider
from auspex.providers.factory import build_default_providers
from auspex.providers.finnhub import FinnhubNewsProvider
from auspex.settings import Settings


class FakeSecretResolver:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    async def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise LookupError(f"secret {name!r} not found")
        return self._secrets[name]


class TestBuildDefaultProviders:
    @pytest.mark.asyncio
    async def test_defaults_resolve_alpha_vantage_and_finnhub_secret_names(self):
        settings = Settings()
        assert settings.price_api_key_secret == "ALPHAVANTAGE-API-KEY"
        assert settings.news_api_key_secret == "FINNHUB-API-KEY"

        resolver = FakeSecretResolver({"ALPHAVANTAGE-API-KEY": "av-key", "FINNHUB-API-KEY": "fh-key"})
        providers = await build_default_providers(settings, resolver)

        assert isinstance(providers.price_and_fx, AlphaVantageProvider)
        assert isinstance(providers.news, FinnhubNewsProvider)
        assert providers.edgar is not None

    @pytest.mark.asyncio
    async def test_one_provider_serves_both_price_and_fx_interfaces(self):
        settings = Settings()
        resolver = FakeSecretResolver({"ALPHAVANTAGE-API-KEY": "av-key", "FINNHUB-API-KEY": "fh-key"})
        providers = await build_default_providers(settings, resolver)

        assert hasattr(providers.price_and_fx, "get_daily_prices")
        assert hasattr(providers.price_and_fx, "get_usd_chf")

    @pytest.mark.asyncio
    async def test_missing_price_secret_degrades_to_none_not_an_exception(self):
        settings = Settings()
        resolver = FakeSecretResolver({"FINNHUB-API-KEY": "fh-key"})  # price secret absent
        providers = await build_default_providers(settings, resolver)

        assert providers.price_and_fx is None
        assert providers.news is not None  # unaffected by the price provider's failure

    @pytest.mark.asyncio
    async def test_missing_news_secret_degrades_to_none_not_an_exception(self):
        settings = Settings()
        resolver = FakeSecretResolver({"ALPHAVANTAGE-API-KEY": "av-key"})  # news secret absent
        providers = await build_default_providers(settings, resolver)

        assert providers.news is None
        assert providers.price_and_fx is not None

    @pytest.mark.asyncio
    async def test_edgar_client_never_requires_a_secret(self):
        settings = Settings()
        resolver = FakeSecretResolver({})  # no secrets available at all
        providers = await build_default_providers(settings, resolver)

        assert providers.edgar is not None
        assert providers.price_and_fx is None
        assert providers.news is None

    @pytest.mark.asyncio
    async def test_custom_secret_names_from_env_are_honoured(self):
        settings = Settings(price_api_key_secret="CUSTOM-PRICE-SECRET", news_api_key_secret="CUSTOM-NEWS-SECRET")
        resolver = FakeSecretResolver({"CUSTOM-PRICE-SECRET": "p", "CUSTOM-NEWS-SECRET": "n"})
        providers = await build_default_providers(settings, resolver)

        assert providers.price_and_fx is not None
        assert providers.news is not None
