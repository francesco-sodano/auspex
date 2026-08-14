"""Finnhub company news provider (default `NewsProvider` implementation)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from auspex.models.common import sha256_hex
from auspex.providers.base import NewsArticleDTO
from auspex.providers.rate_limit import TokenBucket, backoff_sleep

MAX_RETRIES = 4


class FinnhubNewsProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        rate_limit_per_second: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._bucket = TokenBucket(rate_limit_per_second)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_news(self, ticker: str, since: datetime) -> list[NewsArticleDTO]:
        url = f"{self._base_url}/company-news"
        params = {
            "symbol": ticker,
            "from": since.date().isoformat(),
            "to": date.today().isoformat(),
            "token": self._api_key,
        }
        response = None
        for attempt in range(MAX_RETRIES):
            await self._bucket.acquire()
            response = await self._client.get(url, params=params)
            if response.status_code != 429:
                break
            if attempt == MAX_RETRIES - 1:
                break
            await backoff_sleep(attempt, base_seconds=1, max_seconds=15)
        assert response is not None
        response.raise_for_status()
        rows = response.json()
        return [self._to_dto(ticker, row) for row in rows]

    @staticmethod
    def _to_dto(ticker: str, row: dict) -> NewsArticleDTO:
        published_at = datetime.fromtimestamp(row["datetime"], tz=UTC)
        body = row.get("summary", "")
        content_hash = f"sha256:{sha256_hex(row.get('headline', '') + body)}"
        return NewsArticleDTO(
            ticker=ticker,
            external_id=str(row["id"]),
            title=row.get("headline", ""),
            url=row.get("url", ""),
            published_at=published_at,
            content_hash=content_hash,
            body_text=body,
        )
