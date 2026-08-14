from datetime import date
from decimal import Decimal

from auspex.models.enums import Action, CohortConfidence, Direction, FilerProfile
from auspex.models.scoring import ScoreSnapshot
from auspex.pipeline.steps import _consecutive_weakening_sessions, _suggested_trade_quantity


def snapshot(as_of_date: date, direction: Direction) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"sec:{as_of_date.isoformat()}",
        security_id="sec",
        as_of_date=as_of_date,
        config_version_id="test",
        cohort_used="test",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1",
        legs={},
        composite="0",
        percentile=50,
        direction=direction,
        package_fingerprint="test",
        max_knowledge_date=as_of_date,
    )


def test_trim_returns_executable_whole_share_quantity() -> None:
    quantity = _suggested_trade_quantity(
        Action.TRIM,
        Decimal("11213"),
        Decimal("220"),
        Decimal("0.82"),
        Decimal("100"),
    )

    assert quantity == Decimal("62")


def test_sell_returns_the_complete_held_quantity() -> None:
    quantity = _suggested_trade_quantity(
        Action.SELL,
        Decimal("11213"),
        Decimal("220"),
        Decimal("0.82"),
        Decimal("40.5"),
    )

    assert quantity == Decimal("40.5")


def test_hold_has_no_suggested_quantity() -> None:
    assert (
        _suggested_trade_quantity(
            Action.HOLD_NO_ACTION,
            Decimal("11213"),
            Decimal("220"),
            Decimal("0.82"),
            Decimal("40"),
        )
        is None
    )


def test_weakening_streak_uses_current_and_prior_sessions() -> None:
    prior = [
        snapshot(date(2026, 8, 10), Direction.STABLE),
        snapshot(date(2026, 8, 12), Direction.WEAKENING),
        snapshot(date(2026, 8, 11), Direction.WEAKENING),
    ]

    assert _consecutive_weakening_sessions(Direction.WEAKENING, prior) == 3
    assert _consecutive_weakening_sessions(Direction.STABLE, prior) == 0
