"""Route dependencies that resolve the signed-in user from the session cookie."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.sessions import SESSION_COOKIE, load_session
from easytodo.db import get_db
from easytodo.errors import NotAuthenticated
from easytodo.models import User, UserSession, UserStatus

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def current_session(request: Request, db: DbSession) -> UserSession | None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    return await load_session(db, raw_token) if raw_token else None


async def require_user(request: Request, db: DbSession) -> User:
    """The signed-in, active user; also sets ``request.state.user`` and ``request.state.session``
    for the templates."""
    session = await current_session(request, db)
    if session is None:
        raise NotAuthenticated()
    user = await db.get_one(User, session.user_id)
    if user.status == UserStatus.DISABLED:
        raise NotAuthenticated()
    request.state.user = user
    request.state.session = session
    return user
