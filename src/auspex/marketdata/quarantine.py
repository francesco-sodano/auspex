"""Quarantine helpers shared by storage adapters and in-memory fixtures.

Quarantined bars stay in the container — the raw observation is never deleted —
but they are excluded from every scoring, performance and API read until a
repair pass releases them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

QUARANTINE_SQL_PREDICATE = "(NOT IS_DEFINED(c.quarantined) OR c.quarantined = false)"
"""Cosmos filter that keeps rows written before the field existed."""


def is_quarantined(bar: Any) -> bool:
    """True when a bar is quarantined, tolerating rows without the field."""

    if isinstance(bar, dict):
        return bool(bar.get("quarantined", False))
    return bool(getattr(bar, "quarantined", False))


def exclude_quarantined[T](bars: Iterable[T]) -> list[T]:
    """Drop quarantined bars from an already-materialised sequence."""

    return [bar for bar in bars if not is_quarantined(bar)]


def quarantined_only[T](bars: Sequence[T]) -> list[T]:
    return [bar for bar in bars if is_quarantined(bar)]
