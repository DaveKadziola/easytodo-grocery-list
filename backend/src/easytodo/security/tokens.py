"""Random tokens (sessions, CSRF, invites, password resets); only their hashes are stored.

Also the mask a page carries the CSRF token in. This module imports nothing of the app: ``web``
(which masks) and ``security.csrf`` (which unmasks) both reach it without a cycle.
"""

import base64
import binascii
import hashlib
import re
import secrets

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mask_token(token: str) -> str:
    """``token`` in a form that is new on every call, for the page that carries it.

    A page that repeats a secret byte for byte lets BREACH (a guess about compressed, encrypted
    pages) recover it. Here the page holds a random pad followed by the token XOR the pad
    (base64url, without ``=``): different every time, and ``unmask_token`` gets the token back.
    """
    raw = token.encode()
    pad = secrets.token_bytes(len(raw))
    hidden = (int.from_bytes(pad) ^ int.from_bytes(raw)).to_bytes(len(raw))
    return base64.urlsafe_b64encode(pad + hidden).rstrip(b"=").decode()


def unmask_token(masked: str) -> str | None:
    """The token that ``mask_token`` hid in ``masked``; ``None`` for anything else (empty, not
    base64url, an odd number of bytes, a token that is not ASCII). Never raises: what comes in
    is whatever a client sent."""
    if not _BASE64URL.fullmatch(masked):
        return None
    try:
        data = base64.urlsafe_b64decode(masked + "=" * (-len(masked) % 4))
    except (binascii.Error, ValueError):
        return None
    half, odd = divmod(len(data), 2)
    if odd:
        return None
    raw = (int.from_bytes(data[:half]) ^ int.from_bytes(data[half:])).to_bytes(half)
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None
