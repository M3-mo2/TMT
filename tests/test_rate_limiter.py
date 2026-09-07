"""Offline tests for ``app.core.rate_limiter`` — TokenBucket.

Tests use a monkeypatched ``asyncio.sleep`` (no-op) so the sliding-window
logic advances via the limiter's internal ``time.monotonic`` clock.  Real
sleep is used only for the concurrency test (sub-millisecond work per task).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.rate_limiter import TokenBucket


# --------------------------------------------------------------------------- #
# Throughput / spacing
# --------------------------------------------------------------------------- #


async def test_rate_limit_spaced_acquires(monkeypatch) -> None:
    """With max_per_second=2, concurrency=1, 6 sequential acquires trigger 2 sleeps.

    The first two acquires fill the 1-second window; the 3rd must wait ~1 s,
    the 4th is instant, the 5th waits again, and the 6th is instant.
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    bucket = TokenBucket(max_per_second=2, concurrency=1)
    for _ in range(6):
        await bucket.acquire()
        bucket.release()

    assert len(sleep_calls) == 2
    for s in sleep_calls:
        assert 0.9 <= s <= 1.1


async def test_rate_limit_one_per_second(monkeypatch) -> None:
    """With max_per_second=1, every acquire after the first sleeps ~1 s."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    bucket = TokenBucket(max_per_second=1, concurrency=1)
    for _ in range(5):
        await bucket.acquire()
        bucket.release()

    # First acquire: no sleep.  Each subsequent: one sleep of ~1.0 s.
    assert len(sleep_calls) == 4
    for s in sleep_calls:
        assert 0.9 <= s <= 1.1


async def test_no_sleep_under_limit(monkeypatch) -> None:
    """Acquiring fewer than max_per_second times in one window sleeps zero times."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    bucket = TokenBucket(max_per_second=10, concurrency=1)
    for _ in range(5):
        await bucket.acquire()
        bucket.release()

    assert sleep_calls == []


async def test_rate_limit_real_timing() -> None:
    """End-to-end: real sleep verifies acquires are actually spaced ~1 s apart."""
    bucket = TokenBucket(max_per_second=1, concurrency=1)
    timestamps: list[float] = []
    for _ in range(5):
        await bucket.acquire()
        timestamps.append(time.monotonic())
        bucket.release()

    # First acquire is instant; each subsequent is >= 0.9 s later.
    assert timestamps[1] - timestamps[0] >= 0.9
    assert timestamps[2] - timestamps[1] >= 0.9
    assert timestamps[3] - timestamps[2] >= 0.9
    assert timestamps[4] - timestamps[3] >= 0.9


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


async def test_concurrency_semaphore_caps_simultaneous() -> None:
    """Only ``concurrency`` coroutines can hold the slot at once.

    Workers use *real* sleep for their simulated work so the semaphore is
    actually held; the rate limit (max_per_second=1000) never triggers.
    """
    concurrency = 3
    bucket = TokenBucket(max_per_second=1000, concurrency=concurrency)
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        await bucket.acquire()
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        bucket.release()

    await asyncio.gather(*[worker() for _ in range(10)])
    assert max_active <= concurrency


async def test_concurrency_allows_parallel_under_limit() -> None:
    """With concurrency=4 and max_per_second=1000, 4 workers run at once."""
    bucket = TokenBucket(max_per_second=1000, concurrency=4)
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        await bucket.acquire()
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        bucket.release()

    await asyncio.gather(*[worker() for _ in range(4)])
    assert max_active == 4


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #


async def test_shutdown_doesnt_raise() -> None:
    """shutdown() on a fresh bucket must not raise."""
    bucket = TokenBucket(max_per_second=10, concurrency=5)
    await bucket.shutdown()


async def test_shutdown_makes_acquire_unsafe() -> None:
    """After shutdown, acquire() raises RuntimeError."""
    bucket = TokenBucket(max_per_second=10, concurrency=5)
    await bucket.acquire()
    await bucket.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        await bucket.acquire()
    bucket.release()
