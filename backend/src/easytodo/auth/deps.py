"""The signed-in user from the session cookie: route dependencies, and the same rule for the
board WebSocket (``websocket_user``)."""

from typing import Annotated

from fastapi import Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.sessions import SESSION_COOKIE, load_session, load_session_and_user
from easytodo.db import get_db
from easytodo.errors import (
    Forbidden,
    NotAuthenticated,
    PasswordChangeRequired,
    SecondFactorRequired,
)
from easytodo.models import User, UserSession, UserStatus

DbSession = Annotated[AsyncSession, Depends(get_db)]
FormField = Annotated[str, Form()]

PASSWORD_CHANGE_PATH = "/account/password"
# The second sign-in step: the one page, besides /logout, a session awaiting its code can use.
SECOND_FACTOR_PATH = "/login/2fa"
# The only paths a user who must change their password can still use.
_ALLOWED_BEFORE_PASSWORD_CHANGE = {PASSWORD_CHANGE_PATH, "/logout"}


async def current_session(request: Request, db: DbSession) -> UserSession | None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    return await load_session(db, raw_token) if raw_token else None


async def active_session(
    db: AsyncSession, raw_token: str | None
) -> tuple[User, UserSession] | None:
    """The user and unexpired session of the session cookie ``raw_token``, unless the user is
    disabled. The rule ``require_user`` and ``websocket_user`` share."""
    found = await load_session_and_user(db, raw_token) if raw_token else None
    if found is None:
        return None
    session, user = found
    if user.status == UserStatus.DISABLED:
        return None
    return user, session


async def require_user(request: Request, db: DbSession) -> User:
    """The signed-in, active user; also sets ``request.state.user`` and ``request.state.session``
    for the templates.

    A session still waiting for its second factor gets the code form for a GET and
    ``NotAuthenticated`` for anything else, on every path. A user who must change their password
    gets the password form for a GET and ``Forbidden`` for anything else, except on the paths in
    ``_ALLOWED_BEFORE_PASSWORD_CHANGE``.
    """
    signed_in = await active_session(db, request.cookies.get(SESSION_COOKIE))
    if signed_in is None:
        raise NotAuthenticated()
    user, session = signed_in
    if session.mfa_pending:  # first: the password alone does not reach the password form either
        if request.method in ("GET", "HEAD"):
            raise SecondFactorRequired(SECOND_FACTOR_PATH)
        raise NotAuthenticated()
    request.state.user = user
    request.state.session = session
    if user.must_change_password and request.url.path not in _ALLOWED_BEFORE_PASSWORD_CHANGE:
        if request.method == "GET":
            raise PasswordChangeRequired(PASSWORD_CHANGE_PATH)
        raise Forbidden("You must change your password first.")
    return user


CurrentUser = Annotated[User, Depends(require_user)]


async def pending_sign_in(request: Request, db: DbSession) -> tuple[User, UserSession] | None:
    """The user and session of the cookie if the session still waits for its second factor
    (the second sign-in step's own rule; every other route uses ``require_user``)."""
    signed_in = await active_session(db, request.cookies.get(SESSION_COOKIE))
    if signed_in is None or not signed_in[1].mfa_pending:
        return None
    return signed_in


PendingSignIn = Annotated[tuple[User, UserSession] | None, Depends(pending_sign_in)]


async def websocket_user(
    db: AsyncSession, raw_token: str | None
) -> tuple[User, UserSession] | None:
    """``active_session`` for a WebSocket, which cannot be redirected: a session still waiting
    for its second factor, or a user who must change their password first, counts as signed out
    (``None``)."""
    signed_in = await active_session(db, raw_token)
    if signed_in is None:
        return None
    user, session = signed_in
    if session.mfa_pending or user.must_change_password:
        return None
    return signed_in
