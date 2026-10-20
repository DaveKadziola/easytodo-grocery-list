"""Route dependencies that resolve the signed-in user from the session cookie."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.sessions import SESSION_COOKIE, load_session
from easytodo.db import get_db
from easytodo.errors import Forbidden, NotAuthenticated, PasswordChangeRequired
from easytodo.models import User, UserSession, UserStatus

DbSession = Annotated[AsyncSession, Depends(get_db)]

PASSWORD_CHANGE_PATH = "/account/password"
# The only paths a user who must change their password can still use.
_ALLOWED_BEFORE_PASSWORD_CHANGE = {PASSWORD_CHANGE_PATH, "/logout"}


async def current_session(request: Request, db: DbSession) -> UserSession | None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    return await load_session(db, raw_token) if raw_token else None


async def require_user(request: Request, db: DbSession) -> User:
    """The signed-in, active user; also sets ``request.state.user`` and ``request.state.session``
    for the templates.

    A user who must change their password gets the password form for a GET and ``Forbidden`` for
    anything else, except on the paths in ``_ALLOWED_BEFORE_PASSWORD_CHANGE``.
    """
    session = await current_session(request, db)
    if session is None:
        raise NotAuthenticated()
    user = await db.get_one(User, session.user_id)
    if user.status == UserStatus.DISABLED:
        raise NotAuthenticated()
    request.state.user = user
    request.state.session = session
    if user.must_change_password and request.url.path not in _ALLOWED_BEFORE_PASSWORD_CHANGE:
        if request.method == "GET":
            raise PasswordChangeRequired(PASSWORD_CHANGE_PATH)
        raise Forbidden("You must change your password first.")
    return user
