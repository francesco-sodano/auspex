"""Market-data integrity thresholds (arc42 §5.3).

Every threshold is explicit, Decimal-typed and versioned: ``POLICY_VERSION``
is recorded on each repair manifest revision so a historical manifest can be
interpreted under the exact policy that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

POLICY_VERSION = "market-data-integrity-1"


@dataclass(frozen=True)
class IntegrityPolicy:
    """Detector thresholds and repair behaviour."""

    max_abs_daily_return: Decimal = Decimal("0.45")
    """Single-session move (corporate-action adjusted) above which a bar is suspicious.

    Deliberately just under 50% so an unrecorded 2:1 split — the most common
    scale break — trips the check, while ordinary earnings gaps do not.
    """

    extreme_abs_daily_return: Decimal = Decimal("5")
    """Single-session move above which a bar is unusable and must be quarantined."""

    max_abs_forward_return: Decimal = Decimal("10")
    """Forward return magnitude above which the window is treated as broken data."""

    forward_return_horizons: tuple[int, ...] = (21, 63, 126)
    """Trading-day horizons checked for forward-return anomalies."""

    adjusted_tolerance: Decimal = Decimal("0.002")
    """Relative deviation of the stored adjusted series that triggers a repair."""

    factor_tolerance: Decimal = Decimal("0.002")
    """Relative deviation between ``adjustment_factor`` and ``close_adjusted/close_raw``."""

    convention_tolerance: Decimal = Decimal("0.01")
    """Median deviation under which a provider adjustment convention is considered detected."""

    split_ratio_tolerance: Decimal = Decimal("0.05")
    """Relative distance to a round split ratio for a scale break to look split-like."""

    min_split_ratio: Decimal = Decimal("1.9")
    """Smallest raw-price ratio considered a candidate split/reverse-split.

    Kept at ~2:1 so a genuine (if brutal) single-session repricing is reported
    as an implausible jump rather than mistaken for an unrecorded split.
    """

    candidate_split_ratios: tuple[Decimal, ...] = (
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
        Decimal("5"),
        Decimal("6"),
        Decimal("7"),
        Decimal("8"),
        Decimal("10"),
        Decimal("15"),
        Decimal("20"),
        Decimal("25"),
        Decimal("30"),
        Decimal("40"),
        Decimal("50"),
        Decimal("100"),
    )
    """Round ratios an unexplained scale break is matched against (both directions)."""

    quarantine_history_before_scale_break: bool = False
    """Quarantine history only after an operator enables corroborated scale repair."""

    include_dividends_default: bool = True
    """Convention assumed when neither candidate series matches the stored one."""

    adjusted_quantum: Decimal = field(default=Decimal("0.000001"), compare=False)
    """Rounding grid for a repaired ``close_adjusted``."""

    factor_quantum: Decimal = field(default=Decimal("0.0000000001"), compare=False)
    """Rounding grid for a repaired ``adjustment_factor``."""


DEFAULT_POLICY = IntegrityPolicy()
