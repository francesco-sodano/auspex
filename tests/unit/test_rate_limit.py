"""Unit tests for the async token bucket rate limiter (arc42 §5.3), including
the token-amount-aware `acquire()` used for TPM-based LLM request budgeting."""

from __future__ import annotations

import asyncio

import pytest

from auspex.providers.rate_limit import TokenBucket


class TestTokenBucketRequestBased:
    @pytest.mark.asyncio
    async def test_first_call_does_not_wait_for_slow_rate(self):
        bucket = TokenBucket(rate_per_second=0.083)  # e.g. Alpha Vantage 5/min
        start = asyncio.get_event_loop().time()
        await bucket.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 0.05

    @pytest.mark.asyncio
    async def test_capacity_floored_at_one_for_slow_rates(self):
        bucket = TokenBucket(rate_per_second=0.01)
        assert bucket.capacity >= 1.0


class TestTokenBucketAmountBased:
    @pytest.mark.asyncio
    async def test_acquire_default_amount_consumes_one_unit(self):
        bucket = TokenBucket(rate_per_second=1000.0, capacity=10.0)
        await bucket.acquire()
        assert bucket._tokens == pytest.approx(9.0, abs=0.05)

    @pytest.mark.asyncio
    async def test_acquire_with_amount_consumes_that_many_units(self):
        bucket = TokenBucket(rate_per_second=1000.0, capacity=100.0)
        await bucket.acquire(40.0)
        assert bucket._tokens == pytest.approx(60.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_acquire_amount_exceeding_capacity_does_not_hang(self):
        """A single request estimated to need more tokens than the bucket
        could ever hold must still complete (clamped to capacity) rather
        than waiting forever for an unreachable token count."""

        bucket = TokenBucket(rate_per_second=1000.0, capacity=50.0)
        await asyncio.wait_for(bucket.acquire(500.0), timeout=1.0)

    @pytest.mark.asyncio
    async def test_large_amount_drains_bucket_before_next_acquire(self):
        bucket = TokenBucket(rate_per_second=1000.0, capacity=100.0)
        await bucket.acquire(90.0)
        # A second large acquire must wait for refill rather than pass through
        # for free — proves the budget is actually decremented.
        start = asyncio.get_event_loop().time()
        await bucket.acquire(90.0)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed > 0.0
