"""Daily market data and FX (`market_daily` container, arc42 §5.3, §5.11).

Both raw and adjusted price series are stored with the adjustment factor and
effective date. Returns and volatility use adjusted; display uses raw.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from auspex.models.common import AuspexModel


class PriceBar(AuspexModel):
    id: str = Field(description="{security_id}:{session_date}")
    security_id: str
    session_date: date
    open_raw: str
    high_raw: str
    low_raw: str
    close_raw: str
    volume: int
    close_adjusted: str
    adjustment_factor: str = "1"
    split_factor: str = "1"
    dividend_amount: str = "0"

    @property
    def partition_key(self) -> str:
        return self.security_id


class FxRate(AuspexModel):
    id: str = Field(description="USDCHF:{session_date}")
    pair: str = "USDCHF"
    session_date: date
    close_rate: str = Field(description="CHF per 1 USD, Decimal-as-string")

    @property
    def partition_key(self) -> str:
        return self.pair
