"""Shared test steps that drive the app over HTTP."""

import json

import httpx
from factories import DEFAULT_PASSWORD

from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER


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
