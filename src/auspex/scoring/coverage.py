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


def applicable_legs(
    filer_profile: FilerProfile,
    structural_exclusions: frozenset[LegName] | None = None,
) -> tuple[LegName, ...]:
    """Legs that *can* exist for this security.

    ``structural_exclusions`` removes legs that are impossible for this
    particular security rather than merely unevidenced — today that is the
    valuation brake when no point-in-time authoritative FX rate exists to put a
    non-USD reporter's fundamentals on a comparable footing with its peers.
    Excluding them keeps such an issuer from being silently marked down on
    coverage for a leg nobody could have computed. FPI applicability
    (no SMART_MONEY) is unchanged and still comes from ``APPLICABLE_LEGS``.
    """

    legs = APPLICABLE_LEGS[filer_profile]
    if not structural_exclusions:
        return legs
    return tuple(leg for leg in legs if leg not in structural_exclusions)


def coverage(
    computable_legs: set[LegName],
    filer_profile: FilerProfile,
    structural_exclusions: frozenset[LegName] | None = None,
) -> Decimal:
    """computable_legs / applicable_legs (arc42 §5.5).

    Coverage stays an explicit signal, separate from the composite: the
    composite substitutes a neutral ``z = 0`` for an unevidenced leg, and this
    ratio is what records that the leg was in fact unevidenced.
    """

    applicable = applicable_legs(filer_profile, structural_exclusions)
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
