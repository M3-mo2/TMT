"""Token-bucket / sliding-window rate limiter for broadcast sends.

Combines two limits (BroadcastEngine.md §2.5):

1. **Concurrency** — ``asyncio.Semaphore`` caps how many coroutines may be
   simultaneously past the ``acquire`` gate (and therefore mid-send).
2. **Throughput** — a ``deque`` of timestamps enforces ``max_per_second``:
   if the window already holds that many entries within the last second,
   the caller sleeps until the oldest entry falls outside the window.

The limiter is testable with ``asyncio.sleep`` monkeypatched to a no-op:
logical time is tracked via ``time.monotonic``, but after a sleep the
recorded timestamp is advanced to the projected wake time so the window
remains correct even when the wall clock hasn't moved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger("app.core.rate_limiter")

# Minimum sleep duration that is worth honouring.  Microsecond-level sleeps
# arise from floating-point drift when ``asyncio.sleep`` is mocked in tests;
# treating them as zero keeps the sliding window correct without affecting
# real-world timing (1 ms is 0.1 % of a 1-second bucket).
_SLOT_EPSILON = 1e-3


class TokenBucket:
    """Two-dimensional rate limiter: concurrency + per-second throughput."""

    def __init__(self, max_per_second: int, concurrency: int) -> None:
        self._max_per_second = max_per_second
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timestamps: deque[float] = deque()
        self._shutdown = False

    async def acquire(self) -> None:
        """Block until both the concurrency slot and a rate-window slot are free.

        Must be paired with :meth:`release` after the guarded operation
        completes (typically the ``copy_message`` / ``send_message`` call).
        """
        await self._semaphore.acquire()
        if self._shutdown:
            self._semaphore.release()
            raise RuntimeError("TokenBucket is shut down")
        try:
            now = time.monotonic()
            # Advance logical "now" past the last recorded slot.  When
            # asyncio.sleep is mocked the wall clock doesn't move, so without
            # this the deque would keep stale future timestamps.
            if self._timestamps:
                now = max(now, self._timestamps[-1])

            # Evict timestamps that have aged out of the 1-second window.
            while self._timestamps and self._timestamps[0] <= now - 1.0:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_per_second:
                # Bucket is full — wait until the oldest entry exits the window.
                wait_until = self._timestamps[0] + 1.0
                sleep_for = wait_until - now
                if sleep_for > _SLOT_EPSILON:
                    await asyncio.sleep(sleep_for)
                    now = wait_until  # logical time advances with the sleep
                    # Re-evict now that logical time has moved forward.
                    while self._timestamps and self._timestamps[0] <= now - 1.0:
                        self._timestamps.popleft()
                else:
                    # The oldest slot is about to expire — drop it and proceed
                    # immediately (avoids sub-millisecond sleeps from float drift).
                    self._timestamps.popleft()

            self._timestamps.append(now)
        except BaseException:
            self._semaphore.release()
            raise

    def release(self) -> None:
        """Release the concurrency slot acquired by :meth:`acquire`."""
        self._semaphore.release()

    async def shutdown(self) -> None:
        """Signal shutdown and release any internal resources."""
        self._shutdown = True
        # Clear the window so a post-shutdown inspect doesn't see stale state.
        self._timestamps.clear()
        logger.debug("TokenBucket shut down (max_per_second=%d)", self._max_per_second)
