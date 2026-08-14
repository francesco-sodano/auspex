from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from auspex.providers.finnhub import FinnhubNewsProvider


@pytest.mark.asyncio
async def test_retries_rate_limit_then_returns_news(monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "id": 1,
                    "datetime": int(datetime(2026, 8, 11, tzinfo=UTC).timestamp()),
                    "headline": "Marvell expands data-center platform",
                    "summary": "New platform update.",
                    "url": "https://example.test/news",
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FinnhubNewsProvider(
        base_url="https://example.test",
        api_key="test",
        client=client,
        rate_limit_per_second=1000,
    )
    monkeypatch.setattr(
        "auspex.providers.finnhub.backoff_sleep",
        AsyncMock(return_value=None),
    )

    rows = await provider.get_news(
        "MRVL",
        datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert calls == 2
    assert rows[0].ticker == "MRVL"
    assert rows[0].title == "Marvell expands data-center platform"
    await client.aclose()
