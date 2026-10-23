"""Two-factor authentication: a TOTP secret kept encrypted, and single-use recovery codes kept
as salted, slow hashes (scrypt), so that a stolen database or backup gives neither factor.

Every function here that writes commits its change, except ``count_failed_code``.
"""

import hashlib
import hmac
import logging
import re
import secrets

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.config import Settings
from easytodo.errors import Conflict, ValidationFailed
from easytodo.models import RecoveryCode, User, UserSession
from easytodo.security.crypto import DecryptionError, decrypt, encrypt
from easytodo.security.passwords import MAX_PASSWORD_LENGTH, verify_password_async
from easytodo.security.totp import new_totp_secret, verify_totp

logger = logging.getLogger(__name__)

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/i/l to mix up
MAX_CODE_FAILURES = 5  # wrong codes before a pending sign-in is dropped
# Codes one account may try at the second step per window, over all its pending sign-ins and
# from any IP: whoever has the password gets no more guesses by signing in again.
ACCOUNT_CODE_ATTEMPTS = 20
ACCOUNT_CODE_WINDOW_S = 900
# A recovery code has only about 40 bits (8 characters of 31), so a fast hash of it is undone by
# trying them all. scrypt (16 MiB, about 40 ms) with a random salt per set of codes makes that
# years of work for each account, while a sign-in works out one hash: all codes of a set share
# the salt. Not keyed with EASYTODO_SECRET_KEY, so the codes still work after it changes (they
# are the way back in when the TOTP secret can no longer be decrypted).
_RECOVERY_SALT_BYTES = 16
_RECOVERY_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 16}  # stored: salt hex + key hex = 64
_RECOVERY_CODE_SHAPE = re.compile(
    f"[{RECOVERY_CODE_ALPHABET}]{{4}}-[{RECOVERY_CODE_ALPHABET}]{{4}}"
)

INVALID_CODE = "Invalid code. Try again."
WRONG_PASSWORD = "Current password is incorrect."
ALREADY_ON = "Two-factor authentication is already on."
NOT_ON = "Two-factor authentication is off."


async def begin_setup(db: AsyncSession, user: User, settings: Settings) -> str:
    """The TOTP secret to set 2FA up with, stored encrypted while 2FA stays off until
    ``confirm_setup``; returns it (for the QR code).

    A secret from an earlier visit that was not confirmed yet is kept, so reloading the page
    (or a second tab, or the back button) does not replace the key the user has just scanned. A
    new one is made when there is none, or it cannot be decrypted any more.
    """
    if user.totp_enabled:
        raise Conflict(ALREADY_ON)
    secret = _secret(user, settings)
    if secret is not None:
        return secret
    secret = new_totp_secret()
    user.totp_secret_enc = encrypt(secret, settings)
    await db.commit()
    return secret


async def confirm_setup(db: AsyncSession, user: User, code: str, settings: Settings) -> list[str]:
    """Turn 2FA on once ``code`` matches the secret from ``begin_setup``; returns the new recovery
    codes, to be shown this once."""
    if user.totp_enabled:
        raise Conflict(ALREADY_ON)
    secret = _secret(user, settings)
    if secret is None:  # none yet, or no longer decryptable (the caller starts over)
        raise ValidationFailed(INVALID_CODE)
    step = verify_totp(secret, code, user.totp_last_step)
    if step is None:
        raise ValidationFailed(INVALID_CODE)
    user.totp_enabled = True
    user.totp_last_step = step
    codes = await _replace_recovery_codes(db, user)
    await db.commit()
    return codes


async def verify_second_factor(db: AsyncSession, user: User, code: str, settings: Settings) -> bool:
    """Whether ``code`` is a current TOTP code or an unused recovery code of ``user``. Either is
    used up: the code's time step, or the recovery code (atomically, so a code that two requests
    send at once counts for one of them only)."""
    if not user.totp_enabled:
        return False
    secret = _secret(user, settings)
    step = None if secret is None else verify_totp(secret, code, user.totp_last_step)
    if step is not None:
        used = await db.scalar(
            update(User)
            .where(
                User.id == user.id,
                or_(User.totp_last_step.is_(None), User.totp_last_step < step),
            )
            .values(totp_last_step=step)
            .returning(User.id)
        )
    else:
        used = await _use_recovery_code(db, user, code)
    await db.commit()
    return used is not None


async def count_failed_code(db: AsyncSession, session_id: int) -> int | None:
    """Count a wrong code on a pending session (atomically, so parallel attempts all count; not
    committed yet); returns how many it has had, or ``None`` once the session is gone."""
    return await db.scalar(
        update(UserSession)
        .where(UserSession.id == session_id)
        .values(mfa_failures=UserSession.mfa_failures + 1)
        .returning(UserSession.mfa_failures)
    )


async def disable_2fa(
    db: AsyncSession, user: User, password: str, code: str, settings: Settings
) -> None:
    """Turn 2FA off: needs the password and a second factor (a TOTP or a recovery code, so a lost
    phone is no dead end); the secret and the recovery codes are deleted."""
    if not user.totp_enabled:
        raise Conflict(NOT_ON)
    await check_password(user, password)
    if not await verify_second_factor(db, user, code, settings):
        raise ValidationFailed(INVALID_CODE)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    await db.commit()


async def regenerate_recovery_codes(db: AsyncSession, user: User, password: str) -> list[str]:
    """New recovery codes in place of all the old ones; needs the password."""
    if not user.totp_enabled:
        raise Conflict(NOT_ON)
    await check_password(user, password)
    codes = await _replace_recovery_codes(db, user)
    await db.commit()
    return codes


def _secret(user: User, settings: Settings) -> str | None:
    """The user's TOTP secret; ``None`` if there is none or it cannot be decrypted
    (``EASYTODO_SECRET_KEY`` was changed), which leaves the recovery codes to sign in with."""
    if user.totp_secret_enc is None:
        return None
    try:
        return decrypt(user.totp_secret_enc, settings)
    except DecryptionError:
        logger.warning("The TOTP secret of user %s cannot be decrypted.", user.id)
        return None


async def _use_recovery_code(db: AsyncSession, user: User, code: str) -> int | None:
    """Use up ``code`` if it is one of the user's unused recovery codes; returns its id."""
    code = _normalize_recovery_code(code)
    if not _RECOVERY_CODE_SHAPE.fullmatch(code):
        return None  # a TOTP code, say: no scrypt work for what cannot be a recovery code
    unused = await db.execute(
        select(RecoveryCode.id, RecoveryCode.code_hash).where(
            RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
        )
    )
    hashes: dict[str, str] = {}  # salt -> the code's hash with it (one per set of codes)
    for code_id, code_hash in unused:
        salt = code_hash[: _RECOVERY_SALT_BYTES * 2]
        if salt not in hashes:
            hashes[salt] = _hash_recovery_code(code, bytes.fromhex(salt))
        if hmac.compare_digest(hashes[salt], code_hash):
            # Conditional, so a code that two requests send at once counts for one of them only.
            return await db.scalar(
                update(RecoveryCode)
                .where(RecoveryCode.id == code_id, RecoveryCode.used_at.is_(None))
                .values(used_at=func.now())
                .returning(RecoveryCode.id)
            )
    return None


def _hash_recovery_code(code: str, salt: bytes) -> str:
    """``salt`` and the scrypt key of ``code`` with it, as 64 hex characters."""
    return salt.hex() + hashlib.scrypt(code.encode(), salt=salt, **_RECOVERY_SCRYPT).hex()


def recovery_code_matches(code: str, code_hash: str) -> bool:
    """Whether ``code`` (as typed) is the recovery code stored as ``code_hash``."""
    try:
        salt = bytes.fromhex(code_hash[: _RECOVERY_SALT_BYTES * 2])
    except ValueError:
        return False
    return hmac.compare_digest(_hash_recovery_code(_normalize_recovery_code(code), salt), code_hash)


async def check_password(user: User, password: str) -> None:
    """``ValidationFailed`` unless ``password`` is the user's current password."""
    if len(password) > MAX_PASSWORD_LENGTH or not await verify_password_async(
        user.password_hash, password
    ):
        raise ValidationFailed(WRONG_PASSWORD)


async def _replace_recovery_codes(db: AsyncSession, user: User) -> list[str]:
    """New recovery codes in place of the user's old ones (not committed yet); only their hashes
    are stored, all with one new salt."""
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    salt = secrets.token_bytes(_RECOVERY_SALT_BYTES)
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.add_all(
        RecoveryCode(user_id=user.id, code_hash=_hash_recovery_code(code, salt)) for code in codes
    )
    return codes


def _new_recovery_code() -> str:
    """``xxxx-xxxx`` from ``RECOVERY_CODE_ALPHABET``."""
    chars = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _normalize_recovery_code(code: str) -> str:
    """A recovery code as typed (capitals, spaces, no hyphen) in its ``xxxx-xxxx`` form."""
    chars = "".join(code.split()).replace("-", "").lower()
    return f"{chars[:4]}-{chars[4:]}"
