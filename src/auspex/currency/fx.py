"""Explicit dated USD -> CHF conversion (arc42 §8.2).

FX never enters the scoring engine. This module is used exclusively by the
ledger (`auspex.ledger`) and reporting layers, never by `auspex.scoring`.
"""

from __future__ import annotations

from decimal import Decimal

from auspex.currency.money import quantize_money, to_decimal


def convert_usd_to_chf(amount_usd: Decimal | str, rate_chf_per_usd: Decimal | str) -> Decimal:
    """Convert a USD amount to CHF at an explicit dated rate, quantized to cents."""

    amount = to_decimal(amount_usd)
    rate = to_decimal(rate_chf_per_usd)
    return quantize_money(amount * rate)


def fx_effect_chf(
    quantity: Decimal | str,
    price_change_usd: Decimal | str,
    rate_then: Decimal | str,
    rate_now: Decimal | str,
) -> Decimal:
    """Isolate the FX-attributable portion of a CHF value change.

    ``fx_effect_chf`` separates the CHF-denominated move due purely to the
    USD/CHF rate shifting since a lot opened, from the move due to the
    underlying USD price itself (arc42 §5.7 realised P&L).
    """

    qty = to_decimal(quantity)
    delta_price = to_decimal(price_change_usd)
    rate_then_d = to_decimal(rate_then)
    rate_now_d = to_decimal(rate_now)
    # value at cost in USD, revalued at both rates, isolates the FX-only delta
    base_value_usd = qty * delta_price
    return quantize_money(base_value_usd * (rate_now_d - rate_then_d))
