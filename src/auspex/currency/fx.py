"""Explicit dated currency conversion (arc42 §8.2).

Ledger settlement remains USD/CHF. The scoring pipeline may also consume a
point-in-time currency table solely to put non-USD reported fundamentals on a
comparable USD valuation basis.
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
    base_price_usd: Decimal | str,
    rate_then: Decimal | str,
    rate_now: Decimal | str,
) -> Decimal:
    """Isolate the FX-attributable portion of a CHF value change.

    ``base_price_usd`` is the lot's USD cost per share. The result isolates
    ``quantity * base_price * (rate_now - rate_then)``; price movement and the
    price/FX cross term remain outside this component.
    """

    qty = to_decimal(quantity)
    base_price = to_decimal(base_price_usd)
    rate_then_d = to_decimal(rate_then)
    rate_now_d = to_decimal(rate_now)
    base_value_usd = qty * base_price
    return quantize_money(base_value_usd * (rate_now_d - rate_then_d))
