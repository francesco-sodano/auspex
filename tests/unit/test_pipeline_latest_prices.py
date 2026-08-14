from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from auspex.config import load_universe
from auspex.models.market import PriceBar
from auspex.pipeline.steps import _latest_prices_usd


async def test_latest_prices_use_bounded_production_query() -> None:
    security = load_universe().securities[0]

    class PriceSink:
        async def latest_as_of(self, as_of, security_ids):
            assert as_of == date(2026, 8, 10)
            assert security.id in security_ids
            return [
                PriceBar(
                    id=f"{security.id}:2026-08-08",
                    security_id=security.id,
                    session_date=date(2026, 8, 8),
                    open_raw="100",
                    high_raw="100",
                    low_raw="100",
                    close_raw="100",
                    volume=1,
                    close_adjusted="101.5",
                )
            ]

        async def all(self):
            raise AssertionError("full market history must not be loaded")

    ctx = SimpleNamespace(
        as_of_date=date(2026, 8, 10),
        universe=load_universe(),
        repos=SimpleNamespace(price_sink=PriceSink()),
    )

    assert await _latest_prices_usd(ctx) == {security.id: Decimal("101.5")}
