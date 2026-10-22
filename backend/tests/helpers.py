"""Shared test steps that drive the app over HTTP, the name that tests escaping, and waiting for
another connection's row lock."""

import asyncio
import json
from urllib.parse import unquote

import httpx
from factories import DEFAULT_PASSWORD
from sqlalchemy import text

from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.web import FLASH_COOKIE

# A name with quotes, HTML and a script, and how the templates must show it (autoescaped).
NASTY = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
NASTY_ESCAPED = "O&#39;Reilly &lt;b&gt;&#34;x&#34;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;"


async def login(client: httpx.AsyncClient, email: str, password: str = DEFAULT_PASSWORD) -> None:
    """Sign ``client`` in through the login form; later requests carry the CSRF header."""
    await client.get("/login")  # issues the CSRF cookie
    token = client.cookies[CSRF_COOKIE]
    r = await client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}
    )
    assert r.status_code == 303, r.text
    client.headers[CSRF_HEADER] = token


async def signed_in(client_factory, email: str) -> httpx.AsyncClient:
    """A new client (own cookies) from ``client_factory``, signed in as ``email``."""
    client = await client_factory()
    await login(client, email)
    return client


def toast_message(response: httpx.Response) -> str:
    """The error toast of an htmx request's response (its ``HX-Trigger`` header)."""
    return json.loads(response.headers["hx-trigger"])["toast"]["message"]


def flash_of(response: httpx.Response) -> dict:
    """The toast ``response`` leaves for the next page (its flash cookie), as
    ``{"level": ..., "message": ...}``."""
    return json.loads(unquote(response.cookies[FLASH_COOKIE]))


async def wait_until_a_query_waits_for_a_lock(db) -> None:
    """Return once some query on the test database waits for a lock (a request blocked by the row
    lock another connection of the test holds); fail after 5 s."""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with db.bind.connect() as conn:  # a fresh snapshot of pg_stat_activity each time
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no query started waiting for the row lock")
