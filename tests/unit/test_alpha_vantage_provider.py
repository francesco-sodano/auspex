"""Unit tests for the Alpha Vantage price/FX provider."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from auspex.providers.alpha_vantage import AlphaVantageProvider

DAILY_ADJUSTED_FIXTURE = {
    "Time Series (Daily)": {
        "2026-08-07": {
            "1. open": "180.00",
            "2. high": "185.00",
            "3. low": "179.00",
            "4. close": "184.00",
            "5. adjusted close": "184.00",
            "6. volume": "25000000",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "1.0",
        },
        "2026-08-06": {
            "1. open": "178.00",
            "2. high": "181.00",
            "3. low": "177.00",
            "4. close": "179.50",
            "5. adjusted close": "89.75",  # simulates a 2:1 split adjustment
            "6. volume": "30000000",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "2.0",
        },
        "2025-01-01": {  # older than `since` — must be filtered out
            "1. open": "100.00",
            "2. high": "101.00",
            "3. low": "99.00",
            "4. close": "100.50",
            "5. adjusted close": "50.25",
            "6. volume": "1000000",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "2.0",
        },
    }
}

FX_DAILY_FIXTURE = {
    "Time Series FX (Daily)": {
        "2026-08-07": {"1. open": "0.885", "2. high": "0.890", "3. low": "0.880", "4. close": "0.887"},
        "2026-08-06": {"1. open": "0.880", "2. high": "0.886", "3. low": "0.878", "4. close": "0.884"},
    }
}


def make_provider(json_payload: dict, capture: dict | None = None) -> AlphaVantageProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["params"] = dict(request.url.params)
        return httpx.Response(200, json=json_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AlphaVantageProvider(base_url="https://www.alphavantage.co", api_key="test-key", client=client)


class TestGetDailyPrices:
    @pytest.mark.asyncio
    async def test_parses_adjusted_daily_series(self):
        provider = make_provider(DAILY_ADJUSTED_FIXTURE)
        bars = await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))
        assert len(bars) == 2  # the 2025-01-01 row is older than `since` and excluded

    @pytest.mark.asyncio
    async def test_bars_sorted_ascending_by_date(self):
        provider = make_provider(DAILY_ADJUSTED_FIXTURE)
        bars = await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))
        assert [b.session_date for b in bars] == [date(2026, 8, 6), date(2026, 8, 7)]

    @pytest.mark.asyncio
    async def test_adjustment_factor_reflects_split(self):
        provider = make_provider(DAILY_ADJUSTED_FIXTURE)
        bars = await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))
        split_day = next(b for b in bars if b.session_date == date(2026, 8, 6))
        assert split_day.adjustment_factor == Decimal("89.75") / Decimal("179.50")
        assert split_day.split_factor == Decimal("2.0")

    @pytest.mark.asyncio
    async def test_since_filter_excludes_older_rows(self):
        provider = make_provider(DAILY_ADJUSTED_FIXTURE)
        bars = await provider.get_daily_prices("NVDA", since=date(2026, 8, 7))
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 8, 7)

    @pytest.mark.asyncio
    async def test_sends_correct_function_and_symbol(self):
        capture: dict = {}
        provider = make_provider(DAILY_ADJUSTED_FIXTURE, capture=capture)
        await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))
        assert capture["params"]["function"] == "TIME_SERIES_DAILY_ADJUSTED"
        assert capture["params"]["symbol"] == "NVDA"
        assert capture["params"]["apikey"] == "test-key"


class TestGetUsdChf:
    @pytest.mark.asyncio
    async def test_parses_fx_daily_series(self):
        provider = make_provider(FX_DAILY_FIXTURE)
        rates = await provider.get_usd_chf(since=date(2026, 1, 1))
        assert len(rates) == 2
        assert all(r.pair == "USDCHF" for r in rates)

    @pytest.mark.asyncio
    async def test_rates_sorted_ascending(self):
        provider = make_provider(FX_DAILY_FIXTURE)
        rates = await provider.get_usd_chf(since=date(2026, 1, 1))
        assert [r.session_date for r in rates] == [date(2026, 8, 6), date(2026, 8, 7)]

    @pytest.mark.asyncio
    async def test_close_rate_parsed_as_decimal(self):
        provider = make_provider(FX_DAILY_FIXTURE)
        rates = await provider.get_usd_chf(since=date(2026, 8, 7))
        assert rates[0].close_rate == Decimal("0.887")

    @pytest.mark.asyncio
    async def test_sends_correct_function_and_symbols(self):
        capture: dict = {}
        provider = make_provider(FX_DAILY_FIXTURE, capture=capture)
        await provider.get_usd_chf(since=date(2026, 1, 1))
        assert capture["params"]["function"] == "FX_DAILY"
        assert capture["params"]["from_symbol"] == "USD"
        assert capture["params"]["to_symbol"] == "CHF"


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_error_message_raises(self):
        provider = make_provider({"Error Message": "Invalid API call"})
        with pytest.raises(ValueError, match="Alpha Vantage error"):
            await provider.get_daily_prices("BADTICKER", since=date(2026, 1, 1))

    @pytest.mark.asyncio
    async def test_rate_limit_note_raises(self):
        provider = make_provider({"Note": "Thank you for using Alpha Vantage! ..."})
        with pytest.raises(ValueError, match="rate limited"):
            await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))

    @pytest.mark.asyncio
    async def test_information_notice_raises(self):
        # Alpha Vantage returns "Information" (HTTP 200) when a plan lacks the
        # requested premium endpoint — must not be silently treated as empty data.
        provider = make_provider({"Information": "This endpoint requires a premium plan"})
        with pytest.raises(ValueError, match="Alpha Vantage:"):
            await provider.get_daily_prices("NVDA", since=date(2026, 1, 1))
