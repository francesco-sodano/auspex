"""Target weight formula (arc42 §5.6 "Target weight")."""

from __future__ import annotations

from decimal import Decimal

FLOOR_PCT = Decimal("4")
MAX_PCT = Decimal("15")


def target_weight_pct(
    percentile: int | None,
    *,
    max_pct: Decimal = MAX_PCT,
    floor_pct: Decimal = FLOOR_PCT,
) -> Decimal:
    """``15% * (percentile / 100)``, floored at 4%.

    A security at the 80th percentile targets 12%; at the 50th, 7.5%.
    """

    if percentile is None:
        return floor_pct
    raw = max_pct * (Decimal(percentile) / Decimal(100))
    return max(raw, floor_pct)
