"""Time-based one-time passwords (RFC 6238: 6 digits, 30 s steps), as authenticator apps show."""

import hmac
import re
import time

import pyotp
import segno

ISSUER = "EasyToDo"
WINDOW_STEPS = 1  # a code of the step before or after the current one still counts (clock drift)
_CODE = re.compile(r"[0-9]{6}")


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """The ``otpauth://`` URI an authenticator app reads from the QR code."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """``uri`` as a QR code: an ``<svg>`` element to put in the page as is."""
    return segno.make(uri, error="m").svg_inline(scale=4, light="#fff")


def verify_totp(secret: str, code: str, last_step: int | None) -> int | None:
    """The time step whose code ``code`` is, within ``WINDOW_STEPS`` of now; ``None`` if it is
    none of them, or if that step is not after ``last_step`` (a code is used once)."""
    code = "".join(code.split())  # apps show "123 456"
    if not _CODE.fullmatch(code):
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    for step in range(current - WINDOW_STEPS, current + WINDOW_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None
