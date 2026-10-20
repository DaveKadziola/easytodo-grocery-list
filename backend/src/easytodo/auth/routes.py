"""Registration, login and logout pages, and the landing page (``/``)."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.deps import DbSession, current_session, require_user
from easytodo.auth.service import authenticate, register_user
from easytodo.auth.sessions import (
    clear_session_cookie,
    create_session,
    revoke_session,
    set_session_cookie,
)
from easytodo.errors import AppError, NotFound
from easytodo.models import User, UserSession
from easytodo.security.ratelimit import rate_limit
from easytodo.web import render

router = APIRouter()

FormField = Annotated[str, Form()]

# A backslash or an ASCII control character anywhere: browsers read "\" as "/" and drop tabs
# and newlines, so "/\evil.example" and "/\t/evil.example" would leave the site.
_UNSAFE_IN_NEXT = re.compile(r"[\\\x00-\x1f\x7f]")


def safe_next(value: str | None) -> str:
    """``value`` if it is a path on this site, otherwise ``/`` (no open redirect)."""
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and not _UNSAFE_IN_NEXT.search(value)
    ):
        return value
    return "/"


async def require_registration_open(request: Request) -> None:
    if not request.app.state.settings.registration_open:
        raise NotFound()


async def _signed_in_redirect(
    request: Request, db: AsyncSession, user: User, target: str
) -> Response:
    """A 303 to ``target`` that carries the cookie of a new session (a new token each time)."""
    settings = request.app.state.settings
    raw_token, _ = await create_session(
        db,
        user,
        mfa_pending=False,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        settings=settings,
    )
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, raw_token, settings)
    return response


@router.get("/register", dependencies=[Depends(require_registration_open)])
async def register_page(request: Request) -> Response:
    return render(request, "auth/register.html", {"email": ""})


@router.post(
    "/register",
    dependencies=[Depends(require_registration_open), Depends(rate_limit("register", 10, 3600))],
)
async def register(
    request: Request, db: DbSession, email: FormField = "", password: FormField = ""
) -> Response:
    try:
        user = await register_user(db, email, password)
    except AppError as exc:
        context = {"email": email, "error": exc.message}
        return render(request, "auth/register.html", context, status_code=400)
    return await _signed_in_redirect(request, db, user, "/")


@router.get("/login")
async def login_page(request: Request) -> Response:
    return render(request, "auth/login.html", {"email": ""})


@router.post("/login", dependencies=[Depends(rate_limit("login", 20, 300))])
async def login(
    request: Request,
    db: DbSession,
    email: FormField = "",
    password: FormField = "",
    next_: Annotated[str | None, Query(alias="next")] = None,
) -> Response:
    try:
        user = await authenticate(db, email, password)
    except AppError as exc:
        context = {"email": email, "error": exc.message}
        return render(request, "auth/login.html", context, status_code=400)
    return await _signed_in_redirect(request, db, user, safe_next(next_))


@router.post("/logout")
async def logout(
    db: DbSession, session: Annotated[UserSession | None, Depends(current_session)]
) -> Response:
    if session is not None:
        await revoke_session(db, session.id)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


# require_user first: anonymous users go to the login page, and users who must change their
# password to the password form, before anything else.
@router.get("/", dependencies=[Depends(require_user)])
async def home() -> Response:
    return RedirectResponse("/boards", status_code=303)
