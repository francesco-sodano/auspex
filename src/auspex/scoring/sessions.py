"""Trading-session arithmetic (arc42 §5.5 "Direction" / "Staleness exclusion").

Direction, the 7-day delta and the weakening streak are all statements about
*trading sessions*, not calendar days. Comparing ``as_of - timedelta(days=1)``
silently compares Monday against Sunday — a day on which no score exists — and
so reports "no prior value" every Monday and after every market holiday.
Similarly, counting "consecutive weakening sessions" by walking a list of prior
snapshots without checking their dates treats a snapshot from three weeks ago as
if it were yesterday's, which manufactures streaks across gaps.

The functions here are pure: the caller supplies the session calendar (derived
from observed price bars, a data concern) and this module does the arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from auspex.models.enums import Direction


def normalise_calendar(sessions: Sequence[date]) -> tuple[date, ...]:
    """Deduplicate and sort a session calendar ascending."""

    return tuple(sorted(set(sessions)))


def latest_session_on_or_before(calendar: Sequence[date], as_of: date) -> date | None:
    """Most recent session at or before ``as_of``; ``None`` when the calendar is empty."""

    candidates = [d for d in calendar if d <= as_of]
    if not candidates:
        return None
    return max(candidates)


def nth_prior_session(calendar: Sequence[date], as_of: date, n: int) -> date | None:
    """The session ``n`` trading sessions before ``as_of``.

    ``as_of`` itself counts as session 0 when it is a session; when it is not
    (a weekend or holiday run), counting starts from the last session before it.
    Returns ``None`` when the calendar does not reach back far enough — a
    deterministic "unknown", never a silently wrong calendar-day guess.
    """

    if n < 0:
        raise ValueError("n must be non-negative")
    ordered = [d for d in normalise_calendar(calendar) if d <= as_of]
    if not ordered:
        return None
    index = len(ordered) - 1 - n
    if index < 0:
        return None
    return ordered[index]


def prior_sessions(calendar: Sequence[date], as_of: date, count: int) -> tuple[date, ...]:
    """Up to ``count`` sessions strictly before ``as_of``, most recent first."""

    if count <= 0:
        return ()
    ordered = [d for d in normalise_calendar(calendar) if d < as_of]
    return tuple(reversed(ordered[-count:]))


def contiguous_weakening_streak(
    current_direction: Direction,
    directions_by_date: Mapping[date, Direction],
    calendar: Sequence[date],
    as_of: date,
    max_lookback: int = 60,
) -> int:
    """Length of the *contiguous* run of WEAKENING sessions ending at ``as_of``.

    Walks backwards one trading session at a time. The streak stops at the first
    session that is not WEAKENING **and** at the first session for which no
    score exists at all: a missing session breaks contiguity rather than being
    skipped over, so a gap in coverage can never be silently spliced into a
    longer streak than actually occurred.
    """

    if current_direction != Direction.WEAKENING:
        return 0

    streak = 1
    for session in prior_sessions(calendar, as_of, max_lookback):
        direction = directions_by_date.get(session)
        if direction is None or direction != Direction.WEAKENING:
            break
        streak += 1
    return streak
