"""Auth fixtures: an account with two-factor authentication on, and the live server (for the
board WebSocket)."""

from dataclasses import dataclass

import pyotp
import pytest
from factories import create_user
from live_server import live_server  # noqa: F401 (fixture)

from easytodo.auth.twofa import begin_setup, confirm_setup
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
