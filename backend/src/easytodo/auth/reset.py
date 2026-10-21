"""Password reset by e-mail: a link that works once and expires after ``RESET_TTL``; only the
token's hash is stored.

A new request, a reset and a password change each end every earlier link of the account.
Resetting does not turn two-factor authentication off.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.service import normalize_email
from easytodo.auth.sessions import revoke_all_sessions, sessions_revoked
from easytodo.config import Settings
from easytodo.errors import NotFound, ValidationFailed
from easytodo.mail.mailer import Mailer, render_mail
from easytodo.models import PasswordResetToken, User, UserStatus
from easytodo.realtime.events import commit_and_publish
from easytodo.security.passwords import hash_password, validate_password
from easytodo.security.tokens import hash_token, new_token

RESET_TTL = timedelta(minutes=60)
INVALID_RESET_LINK = "This reset link is invalid or has expired."


def _still_usable() -> tuple[ColumnElement[bool], ...]:
    """The conditions under which a reset token can still be used."""
    return PasswordResetToken.used_at.is_(None), PasswordResetToken.expires_at > func.now()


async def invalidate_reset_tokens(db: AsyncSession, user_id: int) -> None:
    """End every reset link of the user that is still unused (not committed yet)."""
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=func.now())
    )


async def request_password_reset(
    db: AsyncSession, email: str, mailer: Mailer, settings: Settings
) -> None:
    """Mail a reset link to the active account with this address (normalised), ending its
    earlier links. Does nothing for an unknown, invalid or disabled address, so the caller
    answers the same either way."""
    try:
        email = normalize_email(email)
    except ValidationFailed:
        return  # no account has an invalid address
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or user.status == UserStatus.DISABLED:
        return
    await invalidate_reset_tokens(db, user.id)
    raw_token = new_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + RESET_TTL,
        )
    )
    await db.commit()
    subject, body = render_mail(
        "password_reset",
        email=user.email,
        link=f"{str(settings.public_url).rstrip('/')}/password/reset/{raw_token}",
        valid_minutes=RESET_TTL // timedelta(minutes=1),
    )
    await mailer.send(user.email, subject, body)


async def get_valid_reset_token(db: AsyncSession, raw_token: str) -> PasswordResetToken:
    """The token for ``raw_token``; ``NotFound`` if it is unknown, expired or used, or its account
    is disabled."""
    token = await db.scalar(
        select(PasswordResetToken)
        .join(User, User.id == PasswordResetToken.user_id)
        .where(
            PasswordResetToken.token_hash == hash_token(raw_token),
            *_still_usable(),
            User.status == UserStatus.ACTIVE,
        )
    )
    if token is None:
        raise NotFound(INVALID_RESET_LINK)
    return token


async def reset_password(db: AsyncSession, raw_token: str, new_password: str) -> User:
    """Give the token's account ``new_password`` and use the token up. Also lifts a lockout and
    a forced password change, and signs the account out on every device (one commit)."""
    token = await get_valid_reset_token(db, raw_token)
    validate_password(new_password)  # before the token is used up, so it can be tried again
    # Conditional, so two concurrent resets cannot both use it.
    used = await db.scalar(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == token.id, *_still_usable())
        .values(used_at=func.now())
        .returning(PasswordResetToken.id)
    )
    if used is None:
        raise NotFound(INVALID_RESET_LINK)
    user = await db.get_one(User, token.user_id)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    await invalidate_reset_tokens(db, user.id)  # a link from a request made at the same time
    revoked = await revoke_all_sessions(db, user.id)
    await commit_and_publish(db, *sessions_revoked(user.id, revoked))  # with the new hash
    return user
