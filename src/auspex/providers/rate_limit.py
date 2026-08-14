"""Async token bucket rate limiter with exponential backoff on 429.

arc42 §5.3: "EDGAR calls go through a token bucket at 8 req/s (below the 10
req/s ceiling) with exponential backoff on 429."
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        self.rate = rate_per_second
        # Default capacity allows an immediate burst of at least one request even for
        # slow steady-state rates (e.g. Alpha Vantage's 5/min free tier = 0.083 req/s):
        # without this floor, a fresh bucket would force every process's *first* call
        # to wait ~(1/rate) seconds before it could even start, which is unnecessary —
        # the throttling only needs to bite on the *second* call onward.
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, amount: float = 1.0) -> None:
        """Acquire ``amount`` units (requests, or — for a token-per-minute
        budget — estimated LLM tokens) from the bucket, waiting as needed.

        ``amount`` is clamped to ``capacity``: a single request estimated to
        need more tokens than the bucket could ever hold (at full refill)
        would otherwise wait forever. It still pays the full cost against
        the bucket up to that ceiling, so it drains the budget rather than
        passing through for free.
        """

        effective_amount = min(amount, self.capacity)
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= effective_amount:
                    self._tokens -= effective_amount
                    return
                wait = (effective_amount - self._tokens) / self.rate
                await asyncio.sleep(wait)


async def backoff_sleep(attempt: int, base_seconds: float = 0.5, max_seconds: float = 30.0) -> None:
    delay = min(max_seconds, base_seconds * (2**attempt))
    await asyncio.sleep(delay)
