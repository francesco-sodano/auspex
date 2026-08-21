from datetime import date
from decimal import Decimal

from auspex.currency.table import PointInTimeFxTable
from auspex.models.market import FxRate


def rate(pair: str, day: str, value: str) -> FxRate:
    return FxRate(
        id=f"{pair}:{day}",
        pair=pair,
        session_date=date.fromisoformat(day),
        close_rate=value,
    )


def test_direct_and_inverse_rates_never_use_the_future():
    table = PointInTimeFxTable(
        [
            rate("EURUSD", "2026-01-02", "1.10"),
            rate("EURUSD", "2026-01-05", "1.20"),
            rate("USDCHF", "2026-01-02", "0.80"),
        ]
    )

    assert table.rate_to_usd("EUR", date(2026, 1, 4)) == Decimal("1.10")
    assert table.rate_to_usd("EUR", date(2026, 1, 5)) == Decimal("1.20")
    assert table.rate_to_usd("CHF", date(2026, 1, 4)) == Decimal("1.25")
    assert table.rate_to_usd("USD", date(2026, 1, 4)) == Decimal(1)


def test_stale_or_missing_rates_are_not_approximated():
    table = PointInTimeFxTable(
        [rate("EURUSD", "2026-01-02", "1.10")],
        max_staleness_days=7,
    )

    assert table.rate_to_usd("EUR", date(2026, 1, 10)) is None
    assert table.rate_to_usd("JPY", date(2026, 1, 2)) is None
