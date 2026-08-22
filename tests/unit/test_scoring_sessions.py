"""Trading-session arithmetic (arc42 §5.5 "Direction" / "Staleness exclusion").

Direction and the weakening streak are statements about *trading sessions*, not
calendar days. These tests pin the two failures that motivated the module: a
calendar-day comparison that lands on a weekend, and a streak assembled from
non-contiguous snapshots.
"""

from __future__ import annotations

from datetime import date

import pytest

from auspex.models.enums import Direction
from auspex.scoring.sessions import (
    contiguous_weakening_streak,
    latest_session_on_or_before,
    normalise_calendar,
    nth_prior_session,
    prior_sessions,
    sessions_between,
)

# Mon 2025-06-02 .. Fri 2025-06-06, then Mon 2025-06-09 .. Wed 2025-06-11.
WEEK = (
    date(2025, 6, 2),
    date(2025, 6, 3),
    date(2025, 6, 4),
    date(2025, 6, 5),
    date(2025, 6, 6),
    date(2025, 6, 9),
    date(2025, 6, 10),
    date(2025, 6, 11),
)


class TestNormaliseCalendar:
    def test_deduplicates_and_sorts(self):
        raw = [date(2025, 6, 4), date(2025, 6, 2), date(2025, 6, 4)]
        assert normalise_calendar(raw) == (date(2025, 6, 2), date(2025, 6, 4))

    def test_empty_calendar_is_empty(self):
        assert normalise_calendar([]) == ()


class TestLatestSessionOnOrBefore:
    def test_returns_the_day_itself_when_it_is_a_session(self):
        assert latest_session_on_or_before(WEEK, date(2025, 6, 4)) == date(2025, 6, 4)

    def test_rolls_back_from_a_weekend(self):
        # Sunday 2025-06-08 rolls back to Friday.
        assert latest_session_on_or_before(WEEK, date(2025, 6, 8)) == date(2025, 6, 6)

    def test_none_before_the_calendar_starts(self):
        assert latest_session_on_or_before(WEEK, date(2025, 6, 1)) is None

    def test_none_for_an_empty_calendar(self):
        assert latest_session_on_or_before((), date(2025, 6, 4)) is None


class TestSessionsBetween:
    """Price age measured in sessions the market actually held.

    This is the input the documented staleness rule expects. Counting calendar
    days instead ages a Friday close by three days over a weekend and by more
    across a holiday, which would exclude perfectly current securities.
    """

    def test_adjacent_sessions_have_nothing_between_them(self):
        assert sessions_between(WEEK, date(2025, 6, 4), date(2025, 6, 5)) == 0

    def test_a_weekend_does_not_age_a_price(self):
        assert sessions_between(WEEK, date(2025, 6, 6), date(2025, 6, 9)) == 0

    def test_counts_only_the_sessions_strictly_in_between(self):
        assert sessions_between(WEEK, date(2025, 6, 2), date(2025, 6, 5)) == 2

    def test_a_same_day_span_is_zero(self):
        assert sessions_between(WEEK, date(2025, 6, 4), date(2025, 6, 4)) == 0

    def test_a_future_dated_bar_is_never_negatively_stale(self):
        assert sessions_between(WEEK, date(2025, 6, 10), date(2025, 6, 4)) == 0

    def test_an_empty_calendar_reports_no_intervening_sessions(self):
        assert sessions_between((), date(2025, 6, 2), date(2025, 6, 11)) == 0

    def test_duplicate_calendar_entries_are_not_double_counted(self):
        noisy = (*WEEK, date(2025, 6, 4), date(2025, 6, 4))
        assert sessions_between(noisy, date(2025, 6, 2), date(2025, 6, 5)) == 2


class TestNthPriorSession:
    def test_zero_is_the_anchor_session(self):
        assert nth_prior_session(WEEK, date(2025, 6, 4), 0) == date(2025, 6, 4)

    def test_one_session_back_skips_the_weekend(self):
        """The regression: Monday's prior session is Friday, not Sunday."""

        assert nth_prior_session(WEEK, date(2025, 6, 9), 1) == date(2025, 6, 6)

    def test_counting_starts_from_the_last_session_when_as_of_is_not_one(self):
        # Saturday: session 0 is Friday, so session 1 is Thursday.
        assert nth_prior_session(WEEK, date(2025, 6, 7), 1) == date(2025, 6, 5)

    def test_none_when_the_calendar_does_not_reach_back_far_enough(self):
        assert nth_prior_session(WEEK, date(2025, 6, 3), 5) is None

    def test_none_for_an_empty_calendar(self):
        assert nth_prior_session((), date(2025, 6, 4), 1) is None

    def test_negative_lookback_is_rejected(self):
        with pytest.raises(ValueError):
            nth_prior_session(WEEK, date(2025, 6, 4), -1)

    def test_unsorted_calendar_is_normalised_first(self):
        shuffled = list(reversed(WEEK))
        assert nth_prior_session(shuffled, date(2025, 6, 9), 1) == date(2025, 6, 6)


class TestPriorSessions:
    def test_returns_sessions_strictly_before_as_of_most_recent_first(self):
        assert prior_sessions(WEEK, date(2025, 6, 5), 3) == (
            date(2025, 6, 4),
            date(2025, 6, 3),
            date(2025, 6, 2),
        )

    def test_excludes_as_of_itself(self):
        assert date(2025, 6, 5) not in prior_sessions(WEEK, date(2025, 6, 5), 8)

    def test_truncates_at_the_start_of_the_calendar(self):
        assert prior_sessions(WEEK, date(2025, 6, 3), 5) == (date(2025, 6, 2),)

    def test_non_positive_count_returns_nothing(self):
        assert prior_sessions(WEEK, date(2025, 6, 5), 0) == ()
        assert prior_sessions(WEEK, date(2025, 6, 5), -2) == ()


class TestContiguousWeakeningStreak:
    def test_zero_when_current_direction_is_not_weakening(self):
        history = {d: Direction.WEAKENING for d in WEEK}
        assert (
            contiguous_weakening_streak(
                Direction.STABLE, history, WEEK, date(2025, 6, 11)
            )
            == 0
        )

    def test_counts_the_current_session(self):
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, {}, WEEK, date(2025, 6, 11)
            )
            == 1
        )

    def test_counts_a_contiguous_run_across_a_weekend(self):
        history = {
            date(2025, 6, 5): Direction.WEAKENING,
            date(2025, 6, 6): Direction.WEAKENING,
            date(2025, 6, 9): Direction.WEAKENING,
            date(2025, 6, 10): Direction.WEAKENING,
        }
        streak = contiguous_weakening_streak(
            Direction.WEAKENING, history, WEEK, date(2025, 6, 11)
        )
        assert streak == 5

    def test_a_non_weakening_session_breaks_the_run(self):
        history = {
            date(2025, 6, 9): Direction.WEAKENING,
            date(2025, 6, 10): Direction.STABLE,
        }
        # Only 2025-06-10 and the current session are inspected before the break.
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, history, WEEK, date(2025, 6, 11)
            )
            == 1
        )

    def test_a_missing_session_breaks_the_run_rather_than_being_skipped(self):
        """The regression: a coverage gap must not be spliced into a streak."""

        history = {
            # 2025-06-10 is absent — no score exists for that session.
            date(2025, 6, 9): Direction.WEAKENING,
            date(2025, 6, 6): Direction.WEAKENING,
            date(2025, 6, 5): Direction.WEAKENING,
        }
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, history, WEEK, date(2025, 6, 11)
            )
            == 1
        )

    def test_non_session_dates_in_history_are_ignored(self):
        """A Sunday snapshot cannot extend a streak."""

        history = {
            date(2025, 6, 8): Direction.WEAKENING,  # Sunday, not a session
            date(2025, 6, 6): Direction.WEAKENING,
        }
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, history, WEEK, date(2025, 6, 9)
            )
            == 2
        )

    def test_max_lookback_bounds_the_walk(self):
        history = {d: Direction.WEAKENING for d in WEEK}
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, history, WEEK, date(2025, 6, 11), max_lookback=2
            )
            == 3
        )

    def test_empty_calendar_yields_only_the_current_session(self):
        assert (
            contiguous_weakening_streak(
                Direction.WEAKENING, {}, (), date(2025, 6, 11)
            )
            == 1
        )
