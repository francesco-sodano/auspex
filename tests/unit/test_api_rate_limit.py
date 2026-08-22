import pytest
from fastapi import HTTPException

from auspex.api.rate_limit import SlidingWindowRateLimiter


@pytest.mark.asyncio
async def test_sliding_window_rate_limit_rejects_and_recovers():
    now = [100.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])

    await limiter.check(
        scope="chat",
        key="user-1",
        limit=2,
        window_seconds=60,
    )
    await limiter.check(
        scope="chat",
        key="user-1",
        limit=2,
        window_seconds=60,
    )
    with pytest.raises(HTTPException) as caught:
        await limiter.check(
            scope="chat",
            key="user-1",
            limit=2,
            window_seconds=60,
        )
    assert caught.value.status_code == 429
    assert caught.value.headers == {"Retry-After": "60"}

    now[0] = 161.0
    await limiter.check(
        scope="chat",
        key="user-1",
        limit=2,
        window_seconds=60,
    )


@pytest.mark.asyncio
async def test_rate_limit_is_isolated_by_scope_and_user():
    limiter = SlidingWindowRateLimiter(clock=lambda: 100.0)
    await limiter.check(
        scope="registration",
        key="user-1",
        limit=1,
        window_seconds=60,
    )
    await limiter.check(
        scope="registration",
        key="user-2",
        limit=1,
        window_seconds=60,
    )
    await limiter.check(
        scope="chat",
        key="user-1",
        limit=1,
        window_seconds=60,
    )


@pytest.mark.asyncio
async def test_non_positive_limit_disables_rate_limiting():
    limiter = SlidingWindowRateLimiter(clock=lambda: 100.0)
    for _ in range(100):
        await limiter.check(
            scope="test",
            key="user-1",
            limit=0,
            window_seconds=60,
        )
