"""Server-side sessions: the cookie carries a random token, the database only its hash.

Every function here that writes commits its change (and anything pending on ``db``), except the
revoke functions: their caller commits the deletion with ``commit_and_publish(db,
*sessions_revoked(...))``, so the WebSockets of those sessions are closed (``realtime.hub``).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import DateTime, String, bindparam, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.config import Settings
from easytodo.models import User, UserSession
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.tokens import hash_token, new_token

SESSION_COOKIE = "easytodo_session"
LAST_SEEN_INTERVAL = timedelta(minutes=5)
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
    """Store a new session for ``user``; returns the raw token (for the cookie) and the row."""
    raw_token = new_token()
    now = datetime.now(UTC)
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        mfa_pending=mfa_pending,
        expires_at=now + timedelta(days=settings.session_ttl_days),
        last_seen_at=now,
        user_agent=user_agent[:_USER_AGENT_MAX_LENGTH] if user_agent else None,
        ip=ip[:_IP_MAX_LENGTH] if ip else None,
    )
    db.add(session)
    await db.commit()
    return raw_token, session


async def _touch(db: AsyncSession, session: UserSession, now: datetime) -> None:
    """Bump ``session.last_seen_at`` to ``now`` and commit, unless it is less than
    ``LAST_SEEN_INTERVAL`` old."""
    if now - session.last_seen_at >= LAST_SEEN_INTERVAL:
        session.last_seen_at = now
        await db.commit()


async def load_session(db: AsyncSession, raw_token: str) -> UserSession | None:
    """The unexpired session for ``raw_token``; bumps ``last_seen_at`` at most every 5 minutes."""
    now = datetime.now(UTC)
    session = await db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_token), UserSession.expires_at > now
        )
    )
    if session is not None:
        await _touch(db, session, now)
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
    user, ``ON DELETE CASCADE``), so there is no moment in which a request finds the session and
    then misses the user.

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
    await _touch(db, session, now)
    return session, user


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    """Delete the session (not committed yet)."""
    await db.execute(delete(UserSession).where(UserSession.id == session_id))


async def revoke_all_sessions(
    db: AsyncSession, user_id: int, *, except_session_id: int | None = None
) -> list[int]:
    """Delete the user's sessions, except ``except_session_id`` (not committed yet); returns
    the deleted ids."""
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
