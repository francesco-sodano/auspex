"""Market-data integrity findings and the versioned repair manifest.

The manifest is an auditable, append-only record of every diagnosis and every
adjusted-series repair applied to the ``market_daily`` container. It lives in
the existing ``config_versions`` container (partition key ``/config_type``)
under its own ``config_type`` discriminator — that container is only ever
point-written and read with explicit partition-scoped queries, so a second
document shape coexists safely with :class:`auspex.models.config_version.ConfigVersion`
without any infrastructure change.

Raw observations are never mutated: a manifest revision records which derived
fields changed, which bars were quarantined (and why), and which
``(security_id, date-range)`` windows downstream consumers must recompute.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from auspex.models.common import AuspexModel

MANIFEST_CONFIG_TYPE = "market_data_repair"


class IntegritySeverity(StrEnum):
    """``ERROR`` findings quarantine the bar; ``WARNING`` findings are recorded only."""

    WARNING = "WARNING"
    ERROR = "ERROR"


class IntegrityCode(StrEnum):
    """Stable, auditable reason codes for every integrity finding."""

    DUPLICATE_BAR = "DUPLICATE_BAR"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    IMPOSSIBLE_OHLC = "IMPOSSIBLE_OHLC"
    IMPOSSIBLE_VOLUME = "IMPOSSIBLE_VOLUME"
    IMPOSSIBLE_ADJUSTED = "IMPOSSIBLE_ADJUSTED"
    IMPOSSIBLE_SPLIT_FACTOR = "IMPOSSIBLE_SPLIT_FACTOR"
    IMPOSSIBLE_DIVIDEND = "IMPOSSIBLE_DIVIDEND"
    ADJUSTED_FACTOR_MISMATCH = "ADJUSTED_FACTOR_MISMATCH"
    ADJUSTED_SERIES_INCONSISTENT = "ADJUSTED_SERIES_INCONSISTENT"
    IMPLAUSIBLE_JUMP = "IMPLAUSIBLE_JUMP"
    UNEXPLAINED_SCALE_BREAK = "UNEXPLAINED_SCALE_BREAK"
    SPLIT_FACTOR_DISCONTINUITY = "SPLIT_FACTOR_DISCONTINUITY"
    STALE_PRICE_SCALE = "STALE_PRICE_SCALE"
    FORWARD_RETURN_ANOMALY = "FORWARD_RETURN_ANOMALY"


class IntegrityFinding(AuspexModel):
    """One diagnosis attached to a security, optionally to a single session."""

    security_id: str
    session_date: date | None = None
    code: IntegrityCode
    severity: IntegritySeverity
    detail: str
    observed: str | None = None
    expected: str | None = None


class BarFieldRepair(AuspexModel):
    """A single derived-field rewrite. Raw fields never appear here."""

    security_id: str
    session_date: date
    field_name: str
    previous: str | None
    repaired: str


class AffectedRange(AuspexModel):
    """Inclusive ``(start_date, end_date)`` window needing recomputation."""

    security_id: str
    start_date: date
    end_date: date
    reason: str


class SecurityIntegrityReport(AuspexModel):
    """Per-security outcome of one diagnose/repair pass."""

    security_id: str
    ticker: str | None = None
    bars_examined: int = 0
    convention: str | None = Field(
        default=None, description="Detected adjustment convention: total_return | split_only"
    )
    findings: list[IntegrityFinding] = Field(default_factory=list)
    repairs: list[BarFieldRepair] = Field(default_factory=list)
    quarantined_dates: list[date] = Field(default_factory=list)
    released_dates: list[date] = Field(default_factory=list)
    affected_ranges: list[AffectedRange] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.repairs or self.quarantined_dates or self.released_dates)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is IntegritySeverity.ERROR)


class MarketDataRepairManifest(AuspexModel):
    """`config_versions` container row, partitioned by `/config_type`."""

    id: str = Field(description="market_data_repair:{revision:06d}")
    config_type: str = Field(default=MANIFEST_CONFIG_TYPE)
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    fingerprint: str = Field(description="sha256 of the deterministic plan payload")
    policy_version: str
    dry_run: bool = False
    securities: list[SecurityIntegrityReport] = Field(default_factory=list)
    securities_examined: int = 0
    bars_examined: int = 0
    bars_repaired: int = 0
    bars_quarantined: int = 0
    bars_released: int = 0

    @property
    def partition_key(self) -> str:
        return self.config_type

    @staticmethod
    def make_id(revision: int) -> str:
        return f"{MANIFEST_CONFIG_TYPE}:{revision:06d}"

    def affected_ranges(self) -> list[AffectedRange]:
        return [rng for report in self.securities for rng in report.affected_ranges]
