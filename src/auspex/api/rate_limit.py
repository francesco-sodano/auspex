"""Bounded per-user sliding-window rate limits for sensitive API operations."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from functools import lru_cache

from fastapi import HTTPException, status


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(
        self,
        *,
        scope: str,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            return
        now = self._clock()
        cutoff = now - window_seconds
        bucket_key = (scope, key)
        async with self._lock:
            events = self._events.setdefault(bucket_key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(
                    1,
                    math.ceil(window_seconds - (now - events[0])),
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"too many {scope} requests; retry later",
                    headers={"Retry-After": str(retry_after)},
                )
            events.append(now)
            if len(self._events) > self._max_keys:
                self._prune_empty(cutoff)

    def _prune_empty(self, cutoff: float) -> None:
        for key, events in list(self._events.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                del self._events[key]


@lru_cache
def get_rate_limiter() -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter()
