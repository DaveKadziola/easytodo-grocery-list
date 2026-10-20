"""Fixed-window rate limiting kept in process memory (``app.state.rate_limiter``)."""

import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

_SWEEP_INTERVAL_S = 60.0


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}  # key -> (window end, hits)
        self._next_sweep = clock() + _SWEEP_INTERVAL_S

    def hit(self, key: str, limit: int, window_s: int) -> bool:
        """Count a hit on ``key``; False once it exceeds ``limit`` hits in the current window.

        A window starts at the key's first hit and lasts ``window_s`` seconds.
        """
        now = self._clock()
        if now >= self._next_sweep:  # drop expired windows so memory stays bounded
            self._windows = {k: w for k, w in self._windows.items() if w[0] > now}
            self._next_sweep = now + _SWEEP_INTERVAL_S
        end, hits = self._windows.get(key, (now, 0))
        if now >= end:
            end, hits = now + window_s, 0
        self._windows[key] = (end, hits + 1)
        return hits < limit


def rate_limit(scope: str, limit: int, window_s: int) -> Callable[[Request], Awaitable[None]]:
    """Route dependency: at most ``limit`` requests per client IP per ``window_s`` seconds."""

    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        if not request.app.state.rate_limiter.hit(f"{scope}:{client_ip}", limit, window_s):
            raise HTTPException(429, "Too many requests. Try again later.")

    return dependency
