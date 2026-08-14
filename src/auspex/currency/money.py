"""Decimal-safe monetary helpers (arc42 TC-06: all monetary values are Decimal)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
BASIS_POINT = Decimal("0.0001")


def quantize_money(value: Decimal, places: Decimal = CENT) -> Decimal:
    """Round to the given number of decimal places using banker-safe half-up."""

    return value.quantize(places, rounding=ROUND_HALF_UP)


def to_decimal(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # route through str() — never construct Decimal directly from a float,
        # which would import binary floating point noise (arc42 TC-06)
        return Decimal(str(value))
    return Decimal(value)


def basis_points_to_rate(bps: Decimal | int) -> Decimal:
    return to_decimal(bps) * BASIS_POINT
