"""Server-side sessions: the cookie carries a random token, the database only its hash.

Every function here that writes commits its change (and anything pending on ``db``).
"""

from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.config import Settings
from easytodo.models import User, UserSession
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


async def load_session(db: AsyncSession, raw_token: str) -> UserSession | None:
    """The unexpired session for ``raw_token``; bumps ``last_seen_at`` at most every 5 minutes."""
    now = datetime.now(UTC)
    session = await db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_token), UserSession.expires_at > now
        )
    )
    if session is not None and now - session.last_seen_at >= LAST_SEEN_INTERVAL:
        session.last_seen_at = now
        await db.commit()
    return session


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    await db.execute(delete(UserSession).where(UserSession.id == session_id))
    await db.commit()


async def revoke_all_sessions(
    db: AsyncSession, user_id: int, *, except_session_id: int | None = None
) -> list[int]:
    """Delete the user's sessions, except ``except_session_id``; returns the deleted ids."""
    statement = delete(UserSession).where(UserSession.user_id == user_id)
    if except_session_id is not None:
        statement = statement.where(UserSession.id != except_session_id)
    revoked = list(await db.scalars(statement.returning(UserSession.id)))
    await db.commit()
    return revoked


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
