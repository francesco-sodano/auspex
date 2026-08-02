from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CostEstimate:
    brokerage_base: Decimal
    spread_base: Decimal
    stamp_duty_base: Decimal
    total_base: Decimal


def estimate_costs(
    notional_base: Decimal,
    *,
    security_country: str | None,
    spread_bps: Decimal = Decimal("5"),
) -> CostEstimate:
    if notional_base < 0:
        raise ValueError("notional must be non-negative")
    if spread_bps < 0:
        raise ValueError("spread_bps must be non-negative")
    brokerage = (
        max(Decimal("5"), notional_base * Decimal("0.0005"))
        if notional_base else Decimal("0")
    )
    spread = notional_base * spread_bps / Decimal("10000")
    stamp_rate = Decimal("0.00075") if security_country == "CH" else Decimal("0.0015")
    stamp = notional_base * stamp_rate
    brokerage = brokerage.quantize(_CENT, rounding=ROUND_HALF_UP)
    spread = spread.quantize(_CENT, rounding=ROUND_HALF_UP)
    stamp = stamp.quantize(_CENT, rounding=ROUND_HALF_UP)
    return CostEstimate(
        brokerage_base=brokerage,
        spread_base=spread,
        stamp_duty_base=stamp,
        total_base=brokerage + spread + stamp,
    )