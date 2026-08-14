"""Coverage and staleness rules (arc42 §5.5 "Coverage" / "Staleness exclusion")."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from auspex.models.enums import FilerProfile, LegName

APPLICABLE_LEGS: dict[FilerProfile, tuple[LegName, ...]] = {
    FilerProfile.DOMESTIC: (
        LegName.THESIS_LINKAGE,
        LegName.ATTENTION_ACCELERATION,
        LegName.NARRATIVE_PREMIUM,
        LegName.SMART_MONEY,
        LegName.FUNDAMENTAL_HEALTH,
        LegName.VALUATION_BRAKE,
    ),
    FilerProfile.FPI: (
        LegName.THESIS_LINKAGE,
        LegName.ATTENTION_ACCELERATION,
        LegName.NARRATIVE_PREMIUM,
        LegName.FUNDAMENTAL_HEALTH,
        LegName.VALUATION_BRAKE,
    ),
}

MIN_COVERAGE_FOR_BUY = Decimal("0.80")
MAX_STALE_SESSIONS = 2


def applicable_legs(filer_profile: FilerProfile) -> tuple[LegName, ...]:
    return APPLICABLE_LEGS[filer_profile]


def coverage(computable_legs: set[LegName], filer_profile: FilerProfile) -> Decimal:
    """computable_legs / applicable_legs (arc42 §5.5)."""

    applicable = applicable_legs(filer_profile)
    if not applicable:
        return Decimal(0)
    computable_count = sum(1 for leg in applicable if leg in computable_legs)
    return Decimal(computable_count) / Decimal(len(applicable))


def is_stale(latest_price_date: date, as_of_date: date, trading_sessions_between: int) -> bool:
    """A security is stale when its latest price is more than 2 trading sessions old.

    ``trading_sessions_between`` must be supplied by the caller (a trading
    calendar concern outside this pure module) as the count of trading
    sessions strictly between ``latest_price_date`` and ``as_of_date``.
    """

    return trading_sessions_between > MAX_STALE_SESSIONS
