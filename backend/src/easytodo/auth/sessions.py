"""Server-side sessions: the cookie carries a random token, the database only its hash.

Every function here that writes commits its change (and anything pending on ``db``), except the
revoke functions and ``rotate_session_token``: their caller commits the change with
``commit_and_publish(db, *sessions_revoked(...))`` (for a revocation, so that the WebSockets of
those sessions are closed, ``realtime.hub``) or with the change that made the rotation necessary.

A transaction that writes more than one of an account's rows across ``users``,
``password_reset_tokens`` and ``user_sessions`` locks the ``users`` row first (see
``revoke_all_sessions``).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import DateTime, String, bindparam, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from easytodo.config import Settings
from easytodo.models import User, UserSession
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.tokens import hash_token, new_token

SESSION_COOKIE = "easytodo_session"
LAST_SEEN_INTERVAL = timedelta(minutes=5)
# How long a sign-in may wait for its code: a session with ``mfa_pending`` expires this long after
# the password was entered. A full session lives ``Settings.session_ttl_days``.
PENDING_SESSION_TTL = timedelta(minutes=15)
_USER_AGENT_MAX_LENGTH = 255  # user_sessions.user_agent is String(255)
_IP_MAX_LENGTH = 45  # user_sessions.ip is String(45)


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    mfa_pending: bool,
    user_agent: str | None,
    ip: str | None,
    settings: Settings,
) -> tuple[str, UserSession]:
    """Store a new session for ``user``; returns the raw token (for the cookie) and the row.

    A session waiting for its second factor expires after ``PENDING_SESSION_TTL``; the cookie
    that carries either kind lasts as long as a full session (the server decides)."""
    raw_token = new_token()
    now = datetime.now(UTC)
    lifetime = PENDING_SESSION_TTL if mfa_pending else timedelta(days=settings.session_ttl_days)
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        mfa_pending=mfa_pending,
        expires_at=now + lifetime,
        last_seen_at=now,
        user_agent=user_agent[:_USER_AGENT_MAX_LENGTH] if user_agent else None,
        ip=ip[:_IP_MAX_LENGTH] if ip else None,
    )
    db.add(session)
    await db.commit()
    return raw_token, session


async def _touch(db: AsyncSession, session: UserSession, now: datetime) -> bool:
    """Bump ``session.last_seen_at`` to ``now`` and commit, unless it is less than
    ``LAST_SEEN_INTERVAL`` old. ``False`` when the session is gone by then (revoked, or its user
    deleted, which takes the session along): the bump's UPDATE matches no row, ``db`` is rolled
    back, and the caller answers like for an expired session. ``True`` otherwise."""
    if now - session.last_seen_at < LAST_SEEN_INTERVAL:
        return True
    session.last_seen_at = now
    try:
        await db.commit()
    except StaleDataError:  # the UPDATE matched 0 rows
        await db.rollback()
        return False
    return True


async def load_session(db: AsyncSession, raw_token: str) -> UserSession | None:
    """The unexpired session for ``raw_token``; bumps ``last_seen_at`` at most every 5 minutes."""
    now = datetime.now(UTC)
    session = await db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_token), UserSession.expires_at > now
        )
    )
    if session is None or not await _touch(db, session, now):
        return None
    return session


# Built once, like the other statements on the hot paths (the token hash and the time are the
# parameters): building one takes longer than running it.
_SESSION_AND_USER = (
    select(UserSession, User)
    .join(User, User.id == UserSession.user_id)
    .where(
        UserSession.token_hash == bindparam("token_hash", type_=String),
        UserSession.expires_at > bindparam("now", type_=DateTime(timezone=True)),
    )
)


async def load_session_and_user(
    db: AsyncSession, raw_token: str
) -> tuple[UserSession, User] | None:
    """The unexpired session for ``raw_token`` and its user, in one query: ``load_session`` plus
    the user, with the same ``last_seen_at`` bump. ``None`` when there is no such session.

    Only that: a disabled user, a session waiting for its second factor and a password that must
    change are for the caller to judge (``deps``). A deleted user has no session (it goes with the
    user, ``ON DELETE CASCADE``), so the one read never finds a session without its user. The
    session can still vanish after that read, before the bump's UPDATE (a logout elsewhere, an
    admin deleting the user): the bump then matches no row and the answer is ``None`` as well
    (see ``_touch``).

    The bump commits after the user was read, so ``db`` must keep loaded objects across a commit
    (the app's sessions do: ``expire_on_commit=False``).
    """
    now = datetime.now(UTC)
    row = (
        await db.execute(_SESSION_AND_USER, {"token_hash": hash_token(raw_token), "now": now})
    ).one_or_none()
    if row is None:
        return None
    session, user = row
    if not await _touch(db, session, now):
        return None
    return session, user


async def rotate_session_token(db: AsyncSession, session_id: int) -> str | None:
    """Give the session a new token, so that the old one, which may have leaked, opens nothing
    any more; returns the raw token for the cookie, or ``None`` if the session is gone (signed
    out from elsewhere meanwhile). Not committed: the caller commits it with the change that made
    the old token worthless (a new password), in one transaction.

    One guarded statement, not a change to the loaded ``UserSession``: that would be flushed as an
    UPDATE that must match its row, and a session deleted in the meantime would fail the whole
    commit (``StaleDataError``) instead of answering ``None``.

    Only ``token_hash`` changes. The row stays the same, with its ``expires_at`` (the WebSockets
    of the session keep theirs and are counted per ``session_id``, ``realtime``), and nothing
    else about the session moves."""
    raw_token = new_token()
    rotated = await db.scalar(
        update(UserSession)
        .where(UserSession.id == session_id)
        .values(token_hash=hash_token(raw_token))
        .returning(UserSession.id)
        .execution_options(synchronize_session="fetch")  # the loaded row only if one matched
    )
    return None if rotated is None else raw_token


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    """Delete the session (not committed yet)."""
    await db.execute(delete(UserSession).where(UserSession.id == session_id))


async def revoke_all_sessions(
    db: AsyncSession, user_id: int, *, except_session_id: int | None = None
) -> list[int]:
    """Delete the user's sessions, except ``except_session_id`` (not committed yet); returns
    the deleted ids.

    Lock order, for every transaction that writes more than one of an account's rows across
    ``users``, ``password_reset_tokens`` and ``user_sessions`` (a password change or reset,
    disabling or deleting the account, signing out the other devices): the ``users`` row first
    (an UPDATE of it, or ``SELECT ... FOR NO KEY UPDATE``), so that they queue on it and none can
    wait for a row the other holds. Writers of one row (logout, one session's revocation, the
    ``last_seen_at`` bump) need no order."""
    statement = delete(UserSession).where(UserSession.user_id == user_id)
    if except_session_id is not None:
        statement = statement.where(UserSession.id != except_session_id)
    return list(await db.scalars(statement.returning(UserSession.id)))


def sessions_revoked(user_id: int, session_ids: Sequence[int]) -> list[BoardEvent]:
    """What to commit a revocation with: the event that closes those sessions' WebSockets
    (nothing when no session was revoked)."""
    if not session_ids:
        return []
    return [BoardEvent(EventType.SESSIONS_REVOKED, user_id=user_id, session_ids=tuple(session_ids))]


def set_session_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=settings.session_ttl_days * 24 * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
