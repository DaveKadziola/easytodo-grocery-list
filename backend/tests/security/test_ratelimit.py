import json

import httpx
import pytest

from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.security.ratelimit import RateLimiter, client_address


def test_rate_limiter_resets_after_window():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert all(rl.hit("k", 5, 60) for _ in range(5))
    assert rl.hit("k", 5, 60) is False
    t[0] = 61
    assert rl.hit("k", 5, 60) is True


def test_rate_limiter_window_is_fixed_not_sliding():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert rl.hit("k", 2, 60) and rl.hit("k", 2, 60)
    t[0] = 59.9
    assert rl.hit("k", 2, 60) is False  # still the window that started at t=0
    t[0] = 60
    assert rl.hit("k", 2, 60) is True  # a new window, not 60 s after the last hit


def test_rate_limiter_counts_keys_separately():
    rl = RateLimiter(clock=lambda: 0.0)
    assert rl.hit("a", 1, 60) is True
    assert rl.hit("a", 1, 60) is False
    assert rl.hit("b", 1, 60) is True


def test_rate_limiter_forgets_expired_windows():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    rl.hit("old", 5, 10)
    t[0] = 3600
    rl.hit("new", 5, 10)
    assert "old" not in rl._windows  # memory stays bounded by the active keys


def _client(app, ip: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 123)),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    )


async def _post_limited(c: httpx.AsyncClient) -> httpx.Response:
    if CSRF_COOKIE not in c.cookies:
        await c.get("/token")
    return await c.post("/limited", headers={CSRF_HEADER: c.cookies[CSRF_COOKIE]})


async def test_rate_limit_dependency_returns_429_per_client_ip(mini_app):
    async with _client(mini_app, "10.0.0.1") as a, _client(mini_app, "10.0.0.2") as b:
        assert [(await _post_limited(a)).status_code for _ in range(2)] == [200, 200]
        r = await _post_limited(a)
        assert r.status_code == 429  # the error page, as for any other error (not JSON)
        assert r.headers["content-type"].startswith("text/html")
        assert "Too many requests. Try again later." in r.text
        r = await a.post(
            "/limited", headers={CSRF_HEADER: a.cookies[CSRF_COOKIE], "HX-Request": "true"}
        )
        assert r.status_code == 429 and r.headers["hx-reswap"] == "none"
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": "Too many requests. Try again later."}
        }
        assert (await _post_limited(b)).status_code == 200  # another IP has its own counter
    assert set(mini_app.state.rate_limiter._windows) == {"test:10.0.0.1", "test:10.0.0.2"}


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("10.0.0.1", "10.0.0.1"),
        ("2001:DB8:1:2::B", "2001:db8:1:2::/64"),  # one /64 is one connection's worth of addresses
        ("::ffff:10.0.0.1", "10.0.0.1"),  # a dual-stack socket's IPv4 client: not the ::/64 block
        ("fe80::1%eth0", "fe80::/64"),
        ("testclient", "testclient"),  # not an address (the test client's host): kept as it is
        (None, "unknown"),
    ],
)
def test_client_address_groups_ipv6_by_64(host, key):
    assert client_address(host) == key


async def test_one_ipv6_64_shares_a_budget(mini_app):
    async def status_from(ip: str) -> int:
        async with _client(mini_app, ip) as c:
            return (await _post_limited(c)).status_code

    # The limit is 2: three addresses of one /64 (whatever their last 64 bits) share it.
    same_64 = ["2001:db8:1:2::a", "2001:db8:1:2:ffff::b", "2001:db8:1:2::a"]
    assert [await status_from(ip) for ip in same_64] == [200, 200, 429]
    # Another /64, and the two IPv4 clients (one of them dual-stack), have budgets of their own.
    others = ["2001:db8:1:3::1", "::ffff:10.0.0.9", "10.0.0.10"]
    assert [await status_from(ip) for ip in others] == [200, 200, 200]
    assert set(mini_app.state.rate_limiter._windows) == {
        "test:2001:db8:1:2::/64",
        "test:2001:db8:1:3::/64",
        "test:10.0.0.9",
        "test:10.0.0.10",
    }
