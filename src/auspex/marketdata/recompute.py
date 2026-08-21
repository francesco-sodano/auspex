"""Targeted recomputation hooks derived from a repair manifest (arc42 §5.3).

Downstream consumers (scoring history, performance attribution) need to know
*which security and which date window* changed. These helpers answer that from
the manifest alone — no price document is re-read — so a recomputation job can
be scheduled without touching the ``market_daily`` container.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from auspex.marketdata.policy import DEFAULT_POLICY, IntegrityPolicy
from auspex.models.market_integrity import AffectedRange, MarketDataRepairManifest


@dataclass(frozen=True)
class RecomputeTarget:
    """Inclusive window of sessions whose derived values are now stale."""

    security_id: str
    start_date: date
    end_date: date
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "security_id": self.security_id,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "reasons": list(self.reasons),
        }


def calendar_lookback_days(trading_days: int) -> int:
    """Convert a trading-day horizon into a safe calendar-day lookback."""

    if trading_days <= 0:
        return 0
    return int(math.ceil(trading_days * 7 / 5)) + 7


def merge_ranges(
    ranges: Iterable[AffectedRange],
    *,
    policy: IntegrityPolicy = DEFAULT_POLICY,
) -> list[RecomputeTarget]:
    """Collapse per-reason ranges into one window per security.

    The start is extended backwards by the longest forward-return horizon: a
    bar repaired on day ``t`` changes the forward return anchored up to
    ``max(horizons)`` sessions earlier, so those anchor dates must be
    recomputed too.
    """

    horizon = max(policy.forward_return_horizons, default=0)
    lookback = timedelta(days=calendar_lookback_days(horizon))

    merged: dict[str, tuple[date, date, set[str]]] = {}
    for entry in ranges:
        start, end, reasons = merged.get(
            entry.security_id, (entry.start_date, entry.end_date, set())
        )
        reasons = set(reasons)
        reasons.add(entry.reason)
        merged[entry.security_id] = (
            min(start, entry.start_date),
            max(end, entry.end_date),
            reasons,
        )

    return [
        RecomputeTarget(
            security_id=security_id,
            start_date=start - lookback,
            end_date=end,
            reasons=tuple(sorted(reasons)),
        )
        for security_id, (start, end, reasons) in sorted(merged.items())
    ]


def targets_from_manifest(
    manifest: MarketDataRepairManifest,
    *,
    policy: IntegrityPolicy = DEFAULT_POLICY,
) -> list[RecomputeTarget]:
    """Recomputation windows implied by one manifest revision."""

    return merge_ranges(manifest.affected_ranges(), policy=policy)
