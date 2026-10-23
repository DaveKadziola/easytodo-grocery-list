"""Password reset by e-mail: a link that works once and expires after ``RESET_TTL``; only the
token's hash is stored.

A new request, a reset, a password change and an admin disabling the account (``set_status``)
each end every earlier link of the account; enabling it again brings none back. Resetting does
not turn two-factor authentication off.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from easytodo.auth.service import normalize_email
from easytodo.auth.sessions import revoke_all_sessions, sessions_revoked
from easytodo.config import Settings
from easytodo.errors import NotFound, ValidationFailed
from easytodo.mail.mailer import Mailer, render_mail
from easytodo.models import PasswordResetToken, User, UserStatus
from easytodo.realtime.events import commit_and_publish
from easytodo.security.passwords import hash_password_async, validate_password
from easytodo.security.ratelimit import RateLimiter
from easytodo.security.tokens import hash_token, new_token

logger = logging.getLogger(__name__)

RESET_TTL = timedelta(minutes=60)
# Mails one account gets from the forgotten-password form per window, whoever asks and from
# wherever: the per-IP limit alone leaves anyone with many addresses free to fill a mailbox.
RESET_MAILS_PER_ACCOUNT = 3
RESET_MAILS_WINDOW_S = 3600
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
    db: AsyncSession,
    email: str,
    mailer: Mailer,
    settings: Settings,
    *,
    limiter: RateLimiter | None = None,
) -> None:
    """Mail a reset link to the active account with this address (normalised), ending its
    earlier links. Does nothing for an unknown, invalid or disabled address, so the caller
    answers the same either way.

    With a ``limiter``, an account gets at most ``RESET_MAILS_PER_ACCOUNT`` mails per
    ``RESET_MAILS_WINDOW_S``; a request over that does nothing either, and ends no link, so that
    a flood of requests cannot make the link already in the mailbox useless. (An admin sending
    a link, who has no use for the cap, passes none.)"""
    try:
        email = normalize_email(email)
    except ValidationFailed:
        return  # no account has an invalid address
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or user.status == UserStatus.DISABLED:
        return
    if limiter is not None and not limiter.hit(
        f"password-reset-mail:user:{user.id}", RESET_MAILS_PER_ACCOUNT, RESET_MAILS_WINDOW_S
    ):
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


async def process_reset_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    email: str,
    mailer: Mailer,
    settings: Settings,
    limiter: RateLimiter,
) -> None:
    """``request_password_reset`` for a background task, which runs once the response is sent:
    in a session of its own (the request's is gone), and whatever goes wrong is logged, never
    raised, as nobody is left to answer. The session holds no connection while the mail is sent
    (it commits before)."""
    try:
        async with sessionmaker() as db:
            await request_password_reset(db, email, mailer, settings, limiter=limiter)
    except Exception:
        logger.exception("Password reset request failed")


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
    new_hash = await hash_password_async(new_password)  # before any row is locked
    # The account's row first, as in ``revoke_all_sessions`` (FOR NO KEY UPDATE): an admin
    # disabling the account locks it first too, and would otherwise wait for the token row below
    # while this waits for the account's.
    user = await db.get(User, token.user_id, with_for_update={"key_share": True})
    if user is None:  # deleted meanwhile
        raise NotFound(INVALID_RESET_LINK)
    # Conditional, so two concurrent resets cannot both use it.
    used = await db.scalar(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == token.id, *_still_usable())
        .values(used_at=func.now())
        .returning(PasswordResetToken.id)
    )
    if used is None:
        raise NotFound(INVALID_RESET_LINK)
    user.password_hash = new_hash
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    await invalidate_reset_tokens(db, user.id)  # a link from a request made at the same time
    revoked = await revoke_all_sessions(db, user.id)
    await commit_and_publish(db, *sessions_revoked(user.id, revoked))  # with the new hash
    return user
