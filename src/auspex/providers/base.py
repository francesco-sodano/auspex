"""Provider abstractions (arc42 §3.1 "Provider abstraction").

``PriceProvider``, ``NewsProvider``, and ``FxProvider`` are interfaces.
Swapping a vendor must require no change outside this package. Named vendors
(Tiingo, Finnhub, exchangerate.host, SEC EDGAR) are the default
implementations in the sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PriceBarDTO:
    ticker: str
    session_date: date
    open_raw: Decimal
    high_raw: Decimal
    low_raw: Decimal
    close_raw: Decimal
    volume: int
    close_adjusted: Decimal
    adjustment_factor: Decimal
    split_factor: Decimal = Decimal(1)
    dividend_amount: Decimal = Decimal(0)


@dataclass(frozen=True)
class FxRateDTO:
    pair: str
    session_date: date
    close_rate: Decimal


@dataclass(frozen=True)
class NewsArticleDTO:
    ticker: str
    external_id: str
    title: str
    url: str
    published_at: datetime
    content_hash: str
    body_text: str | None = None


class PriceProvider(Protocol):
    async def get_daily_prices(self, ticker: str, since: date) -> list[PriceBarDTO]: ...


class FxProvider(Protocol):
    async def get_daily_fx(
        self,
        pair: str,
        since: date,
    ) -> list[FxRateDTO]: ...

    async def get_usd_chf(self, since: date) -> list[FxRateDTO]: ...


class NewsProvider(Protocol):
    async def get_news(self, ticker: str, since: datetime) -> list[NewsArticleDTO]: ...
