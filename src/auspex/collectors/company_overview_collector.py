"""Current provider fundamentals, isolated from historical SEC/scoring inputs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from azure.core.exceptions import AzureError

from auspex.models.common import utc_now
from auspex.models.company_overview import CompanyOverviewSnapshot
from auspex.models.security import Security

logger = logging.getLogger(__name__)
OVERVIEW_REFRESH_AGE = timedelta(hours=24)


class CompanyOverviewProvider(Protocol):
    async def get_company_overview(
        self, security_id: str, ticker: str, previous: CompanyOverviewSnapshot | None = None,
    ) -> CompanyOverviewSnapshot: ...


class CompanyOverviewRepository(Protocol):
    async def get(self, id_: str, partition_key: str) -> CompanyOverviewSnapshot | None: ...

    async def upsert(self, item: CompanyOverviewSnapshot) -> None: ...


@dataclass
class OverviewRefreshResult:
    refreshed: int = 0
    cached: int = 0
    failed_tickers: list[str] = field(default_factory=list)


async def refresh_company_overviews(
    securities: Sequence[Security],
    provider: CompanyOverviewProvider,
    repository: CompanyOverviewRepository,
    *,
    force: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> OverviewRefreshResult:
    result = OverviewRefreshResult()
    for security in securities:
        try:
            previous = await repository.get(security.id, partition_key=security.id)
            age = clock() - previous.retrieved_at if previous is not None else None
            if not force and age is not None and timedelta(0) <= age < OVERVIEW_REFRESH_AGE:
                result.cached += 1
                continue
            snapshot = await provider.get_company_overview(security.id, security.ticker, previous)
            if snapshot.security_id != security.id or snapshot.id != security.id or snapshot.ticker != security.ticker:
                raise ValueError("Provider overview belongs to a different issuer")
            await repository.upsert(snapshot)
            result.refreshed += 1
        except (httpx.HTTPError, AzureError, ValueError, TypeError, KeyError, TimeoutError) as exc:
            # Provider HTTP exceptions can include API-key query parameters.
            logger.error("Company overview refresh failed for %s (%s)", security.ticker, type(exc).__name__)
            result.failed_tickers.append(security.ticker)
        if (result.refreshed + len(result.failed_tickers)) % 10 == 0:
            logger.info(
                "Company overview refresh: refreshed=%d cached=%d failed=%d",
                result.refreshed, result.cached, len(result.failed_tickers),
            )
    return result
