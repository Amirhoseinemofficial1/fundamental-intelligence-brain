from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Simple monotonic start-rate limiter shared inside one service process."""

    def __init__(self, min_gap_seconds: float = 0.0):
        self.min_gap = max(0.0, float(min_gap_seconds))
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self.min_gap <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.min_gap
