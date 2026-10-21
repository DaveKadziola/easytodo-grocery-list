"""An https public URL (as behind the Caddy proxy profile) and what it switches on.

Behind Caddy the app is reached over plain http on the compose network, and uvicorn trusts
the proxy's ``X-Forwarded-*`` headers. Cookie security, HSTS and the expected ``Origin`` follow
``EASYTODO_PUBLIC_URL`` alone, never the scheme a request arrived with.
"""

import httpx
import pytest
from factories import DEFAULT_PASSWORD, create_user

from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.security.csrf import CSRF_COOKIE

HSTS = "max-age=31536000; includeSubDomains"


def _cookie(response: httpx.Response, name: str) -> str:
    return next(c for c in response.headers.get_list("set-cookie") if c.startswith(f"{name}="))


async def test_https_public_url_enables_secure_cookie_and_hsts(https_client, db):
    r = await https_client.get("/login")
    assert "Secure" in r.headers["set-cookie"]
    assert r.headers["strict-transport-security"] == HSTS

    await create_user(db, "dave@example.com")
    r = await https_client.post(
        "/login",
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": https_client.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 303
    assert r.headers["strict-transport-security"] == HSTS
    session_cookie = _cookie(r, SESSION_COOKIE).lower().split("; ")
    assert {"secure", "httponly", "samesite=lax"} <= set(session_cookie)


@pytest.mark.parametrize("forwarded_proto", ["http", "https"])
async def test_forwarded_proto_does_not_override_public_url_origin_check(
    https_client, forwarded_proto
):
    await https_client.get("/login")
    form = {
        "email": "nobody@example.com",
        "password": "not-the-right-password",
        "csrf_token": https_client.cookies[CSRF_COOKIE],
    }
    proxied = {"X-Forwarded-Proto": forwarded_proto}

    r = await https_client.post(
        "/login", data=form, headers=proxied | {"Origin": "http://testserver"}
    )
    assert r.status_code == 403
    assert "Open the app at https://testserver." in r.text

    # The same request from the public https origin passes the CSRF check (and fails the login).
    r = await https_client.post("/login", data=form, headers=proxied)
    assert r.status_code == 400
    assert "Invalid email or password." in r.text
