"""Fixed-window rate limiting kept in process memory (``app.state.rate_limiter``)."""

import ipaddress
import time
from collections.abc import Awaitable, Callable

from fastapi import Request

from easytodo.errors import TooManyRequests

_SWEEP_INTERVAL_S = 60.0
TOO_MANY_REQUESTS = "Too many requests. Try again later."


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


def client_address(host: str | None) -> str:
    """What a client's requests are counted under: its IPv4 address, or its IPv6 /64 network.

    One home or phone connection usually gets a whole /64, so counting each IPv6 address on its
    own would give one client as many budgets as it likes. ``host`` is what the server reports
    (behind the proxy, the forwarded address). An IPv4 client of a dual-stack socket
    (``::ffff:10.0.0.1``) counts as that IPv4 address, not in the ``::/64`` of all of them.
    Something that is not an address (the test client's "testclient") is kept as it is; no host
    at all is "unknown".
    """
    if host is None:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return str(ip.ipv4_mapped)
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    except ValueError:
        return host
    return str(ip)


def rate_limit(scope: str, limit: int, window_s: int) -> Callable[[Request], Awaitable[None]]:
    """Route dependency: at most ``limit`` requests per client address (see ``client_address``)
    per ``window_s`` seconds."""

    async def dependency(request: Request) -> None:
        address = client_address(request.client.host if request.client else None)
        if not request.app.state.rate_limiter.hit(f"{scope}:{address}", limit, window_s):
            raise TooManyRequests(TOO_MANY_REQUESTS)  # the error page or a toast, not JSON

    return dependency


def limit_user(request: Request, scope: str, user_id: int, limit: int, window_s: int) -> None:
    """Count one hit for ``user_id`` in ``scope``; ``TooManyRequests`` (429) once it exceeds
    ``limit`` hits per ``window_s`` seconds.

    For a signed-in action, called in the route when only some of its requests count. Keyed by
    user, not IP, so people behind one address (a household, a proxy) do not share a budget.
    """
    if not request.app.state.rate_limiter.hit(f"{scope}:user:{user_id}", limit, window_s):
        raise TooManyRequests(TOO_MANY_REQUESTS)
