"""Daily market data and FX (`market_daily` container, arc42 §5.3, §5.11).

Both raw and adjusted price series are stored with the adjustment factor and
effective date. Returns and volatility use adjusted; display uses raw.

The raw observation (``open_raw``/``high_raw``/``low_raw``/``close_raw``,
``volume``) together with the authoritative corporate-action fields
(``split_factor``, ``dividend_amount``) is immutable: market-data integrity
repair only ever rewrites the *derived* adjusted series
(``close_adjusted``/``adjustment_factor``), and the provider's original
derived values are preserved once in ``close_adjusted_source`` /
``adjustment_factor_source`` before the first rewrite.
"""

from __future__ import annotations

from datetime import date, datetime

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
    close_adjusted_source: str | None = Field(
        default=None,
        description="Provider-supplied close_adjusted, captured once before the first repair",
    )
    adjustment_factor_source: str | None = Field(
        default=None,
        description="Provider-supplied adjustment_factor, captured once before the first repair",
    )
    quarantined: bool = Field(
        default=False,
        description="Excluded from scoring/performance reads until repaired or released",
    )
    quarantine_codes: list[str] = Field(
        default_factory=list, description="IntegrityCode values that caused quarantine"
    )
    integrity_revision: int = Field(
        default=0, description="Repair manifest revision that last touched this bar"
    )
    repaired_at: datetime | None = None

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
