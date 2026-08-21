"""Point-in-time currency conversion backed by stored daily FX closes."""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import date
from decimal import Decimal

from auspex.models.market import FxRate


class PointInTimeFxTable:
    """Resolve reporting currencies to USD without using future rates."""

    def __init__(
        self,
        rates: list[FxRate],
        *,
        max_staleness_days: int = 7,
    ) -> None:
        self._max_staleness_days = max_staleness_days
        by_pair: dict[str, list[FxRate]] = defaultdict(list)
        for rate in rates:
            by_pair[rate.pair.upper()].append(rate)
        self._rates = {
            pair: sorted(items, key=lambda item: item.session_date)
            for pair, items in by_pair.items()
        }
        self._dates = {
            pair: [item.session_date for item in items]
            for pair, items in self._rates.items()
        }

    def rate_to_usd(
        self,
        currency: str,
        on_date: date,
    ) -> Decimal | None:
        normalized = currency.strip().upper()
        if normalized == "USD":
            return Decimal(1)
        direct = self._latest(f"{normalized}USD", on_date)
        if direct is not None:
            return direct
        inverse = self._latest(f"USD{normalized}", on_date)
        if inverse is None or inverse == 0:
            return None
        return Decimal(1) / inverse

    def _latest(self, pair: str, on_date: date) -> Decimal | None:
        dates = self._dates.get(pair, [])
        index = bisect.bisect_right(dates, on_date) - 1
        if index < 0:
            return None
        session_date = dates[index]
        if (on_date - session_date).days > self._max_staleness_days:
            return None
        return Decimal(self._rates[pair][index].close_rate)
