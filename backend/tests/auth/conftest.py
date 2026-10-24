"""Auth fixtures: an account with two-factor authentication on, clients that come from a given
IP address, and the live server (for the board WebSocket)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx
import pyotp
import pytest
from factories import create_user
from helpers import login
from live_server import live_server  # noqa: F401 (fixture)
from starlette.types import ASGIApp

from easytodo.auth.twofa import begin_setup, confirm_setup
from easytodo.config import Settings
from easytodo.models import User


@dataclass(frozen=True)
class TwoFactorUser:
    user: User
    secret: str  # the TOTP secret, as the authenticator app has it
    recovery_codes: list[str]


@pytest.fixture
async def user_with_2fa(db, settings) -> TwoFactorUser:
    """dave@example.com (password ``DEFAULT_PASSWORD``) with 2FA turned on by the service, and
    no time step used up yet: ``pyotp.TOTP(secret).now()`` completes a sign-in."""
    user = await create_user(db, "dave@example.com")
    secret = await begin_setup(db, user, settings)
    codes = await confirm_setup(db, user, pyotp.TOTP(secret).now(), settings)
    user.totp_last_step = None  # confirming the setup used up the current step
    await db.commit()
    return TwoFactorUser(user, secret, codes)


@pytest.fixture
async def client_from_ip(
    _running_app: ASGIApp, settings: Settings
) -> AsyncIterator[Callable[[str], Awaitable[httpx.AsyncClient]]]:
    """``await client_from_ip("10.0.0.1")`` -> a new client (own cookies) for the app whose
    requests come from that address (the per-IP rate limits key on it)."""
    async with AsyncExitStack() as stack:

        async def factory(ip: str) -> httpx.AsyncClient:
            transport = httpx.ASGITransport(app=_running_app, client=(ip, 123))
            origin = settings.public_origin
            client = httpx.AsyncClient(
                transport=transport, base_url=origin, headers={"Origin": origin}
            )
            return await stack.enter_async_context(client)

        yield factory


@pytest.fixture
async def account_client(client: httpx.AsyncClient, db) -> httpx.AsyncClient:
    """``client`` signed in as a new dave@example.com (all settings at their defaults)."""
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    return client
