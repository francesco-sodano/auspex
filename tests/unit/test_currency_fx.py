from decimal import Decimal

from auspex.currency.fx import fx_effect_chf


def test_fx_effect_uses_base_value_even_when_usd_price_is_flat():
    assert fx_effect_chf(
        quantity="10",
        base_price_usd="100",
        rate_then="0.80",
        rate_now="0.90",
    ) == Decimal("100.00")
