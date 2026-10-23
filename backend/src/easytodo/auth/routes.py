"""Registration, login (password, then the code when 2FA is on), password reset and logout
pages, and the landing page (``/``)."""

import re
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.deps import (
    SECOND_FACTOR_PATH,
    DbSession,
    FormField,
    PendingSignIn,
    current_session,
    require_user,
)
from easytodo.auth.reset import get_valid_reset_token, process_reset_request, reset_password
from easytodo.auth.service import authenticate, normalize_email, register_user
from easytodo.auth.sessions import (
    clear_session_cookie,
    create_session,
    revoke_session,
    sessions_revoked,
    set_session_cookie,
)
from easytodo.auth.twofa import (
    ACCOUNT_CODE_ATTEMPTS,
    ACCOUNT_CODE_WINDOW_S,
    INVALID_CODE,
    MAX_CODE_FAILURES,
    TOO_MANY_CODES,
    reserve_code_attempt,
    verify_second_factor,
)
from easytodo.boards.invites import INVITE_OTHER_EMAIL, get_valid_invite
from easytodo.boards.routes import LAST_BOARD_COOKIE
from easytodo.boards.service import landing_board_id
from easytodo.errors import AppError, NotFound, TooManyRequests, ValidationFailed
from easytodo.models import BoardInvite, User, UserSession
from easytodo.realtime.events import commit_and_publish
from easytodo.security.ratelimit import limit_user, rate_limit
from easytodo.web import flash, no_store, render

router = APIRouter()

# Where to go after signing in or up (a path on this site; see safe_next).
NextParam = Annotated[str | None, Query(alias="next")]

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


# An invite's page, as a ``next`` that may open a closed registration (new_token's alphabet).
_INVITE_PAGE = re.compile(r"/invite/([A-Za-z0-9_-]+)")


async def require_registration_allowed(
    request: Request, db: DbSession, next_: NextParam = None
) -> BoardInvite | None:
    """Registration is open (``None``), or it is closed and ``next`` is the page of an invite
    that is still valid: that invite, so that the person it invites can sign up. Otherwise
    ``NotFound``, which for a dead invite says so. Checked on the submit too: the invite may have
    been revoked since the page was shown."""
    if request.app.state.settings.registration_open:
        return None
    page = _INVITE_PAGE.fullmatch(safe_next(next_))
    if page is None:
        raise NotFound()
    return await get_valid_invite(db, page[1])


RegistrationInvite = Annotated[BoardInvite | None, Depends(require_registration_allowed)]

# Counted in the route, after RegistrationInvite: a post that closed registration refuses with a
# 404 uses up nothing, so a stranger's tries cannot take the budget of the people invited.
_register_attempts = rate_limit("register", 10, 3600)


async def _signed_in_redirect(
    request: Request, db: AsyncSession, user: User, target: str, *, mfa_pending: bool = False
) -> Response:
    """A 303 to ``target`` that carries the cookie of a new session (a new token each time)."""
    settings = request.app.state.settings
    raw_token, _ = await create_session(
        db,
        user,
        mfa_pending=mfa_pending,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        settings=settings,
    )
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, raw_token, settings)
    return response


def _kept_next(next_: str | None) -> str | None:
    """The ``next`` target that the links between the sign-in and sign-up pages pass on."""
    target = safe_next(next_)
    return None if target == "/" else target


@router.get("/register")
async def register_page(
    request: Request, invite: RegistrationInvite, next_: NextParam = None
) -> Response:
    # An e-mail invite's page: its address, which the account must have.
    email = invite.email if invite is not None and invite.email is not None else ""
    return render(request, "auth/register.html", {"email": email, "next_path": _kept_next(next_)})


@router.post("/register")
async def register(
    request: Request,
    db: DbSession,
    invite: RegistrationInvite,
    email: FormField = "",
    password: FormField = "",
    next_: NextParam = None,
) -> Response:
    await _register_attempts(request)  # before the try: its 429 is an AppError, not a form error
    try:
        if (  # an e-mail invite opens sign-up for its own address only
            invite is not None
            and invite.email is not None
            and normalize_email(email) != invite.email
        ):
            raise ValidationFailed(INVITE_OTHER_EMAIL)
        user = await register_user(db, email, password)
    except AppError as exc:
        context = {"email": email, "error": exc.message, "next_path": _kept_next(next_)}
        return render(request, "auth/register.html", context, status_code=400)
    return await _signed_in_redirect(request, db, user, safe_next(next_))


@router.get("/login")
async def login_page(request: Request, next_: NextParam = None) -> Response:
    return render(request, "auth/login.html", {"email": "", "next_path": _kept_next(next_)})


@router.post("/login", dependencies=[Depends(rate_limit("login", 20, 300))])
async def login(
    request: Request,
    db: DbSession,
    previous: Annotated[UserSession | None, Depends(current_session)],
    email: FormField = "",
    password: FormField = "",
    next_: NextParam = None,
) -> Response:
    """Sign in with the password. The browser starts over, so a sign-in it left half-way (the
    session of its cookie still waits for a code, whoever it is for) ends once this one succeeds;
    a wrong password leaves it be, and a full session is never touched."""
    try:
        user = await authenticate(db, email, password)
    except AppError as exc:
        context = {"email": email, "error": exc.message, "next_path": _kept_next(next_)}
        return render(request, "auth/login.html", context, status_code=400)
    if previous is not None and previous.mfa_pending:
        await revoke_session(db, previous.id)
        await commit_and_publish(db, *sessions_revoked(previous.user_id, [previous.id]))
    if user.totp_enabled:  # a session that only reaches the second step, which keeps next
        kept = _kept_next(next_)
        target = SECOND_FACTOR_PATH + (f"?next={quote(kept, safe='')}" if kept else "")
        return await _signed_in_redirect(request, db, user, target, mfa_pending=True)
    return await _signed_in_redirect(request, db, user, safe_next(next_))


@router.get(SECOND_FACTOR_PATH)
async def second_factor_page(request: Request, pending: PendingSignIn) -> Response:
    if pending is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "auth/login_2fa.html", {})


@router.post(SECOND_FACTOR_PATH, dependencies=[Depends(rate_limit("login-2fa", 10, 300))])
async def second_factor(
    request: Request,
    db: DbSession,
    pending: PendingSignIn,
    code: FormField = "",
    next_: NextParam = None,
) -> Response:
    """A right code swaps the pending session for a full one (a new token); the fifth wrong one
    ends the pending session, so the password has to be entered again.

    Every attempt is counted before its code is looked at (``reserve_code_attempt``), so codes
    sent at once are no more guesses than codes sent one by one: only the first five are
    checked, and a later one is refused unchecked, the right code included. Besides the limit per
    IP, the account has a budget of codes over all its pending sessions, which signing in again
    does not renew; an attempt over it is not counted or checked at all."""
    if pending is None:
        return RedirectResponse("/login", status_code=303)
    user, session = pending
    try:
        limit_user(request, "login-2fa", user.id, ACCOUNT_CODE_ATTEMPTS, ACCOUNT_CODE_WINDOW_S)
    except TooManyRequests as exc:  # the code form again, with the reason, for a later try
        return render(request, "auth/login_2fa.html", {"error": exc.message}, status_code=429)
    settings = request.app.state.settings
    attempt = await reserve_code_attempt(db, session.id)
    if attempt is None:  # signed out from elsewhere meanwhile: nothing left to say about codes
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response
    if attempt <= MAX_CODE_FAILURES and await verify_second_factor(db, user, code, settings):
        await revoke_session(db, session.id)
        await commit_and_publish(db, *sessions_revoked(user.id, [session.id]))
        return await _signed_in_redirect(request, db, user, _kept_next(next_) or "/boards")
    if attempt < MAX_CODE_FAILURES:
        return render(request, "auth/login_2fa.html", {"error": INVALID_CODE}, status_code=400)
    await revoke_session(db, session.id)  # the fifth wrong code, or an attempt over the limit
    await commit_and_publish(db, *sessions_revoked(user.id, [session.id]))
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    flash(response, TOO_MANY_CODES, settings, level="error")
    return response


@router.get("/password/forgot")
async def forgot_password_page(request: Request) -> Response:
    return render(request, "auth/forgot.html", {})


@router.post("/password/forgot", dependencies=[Depends(rate_limit("password-forgot", 5, 3600))])
async def forgot_password(
    request: Request, background: BackgroundTasks, email: FormField = ""
) -> Response:
    """The same answer whether or not the address has an account. The account work and the mail
    follow the response, so how long the answer takes tells nothing either, and no database
    session of the request is held while the mail goes out."""
    state = request.app.state
    background.add_task(
        process_reset_request,
        state.sessionmaker,
        email,
        state.mailer,
        state.settings,
        state.rate_limiter,
    )
    notice = "If an account exists for that email, we've sent a reset link."
    return render(request, "auth/forgot.html", {"notice": notice})


@router.get("/password/reset/{token}")
async def reset_password_page(request: Request, db: DbSession, token: str) -> Response:
    await get_valid_reset_token(db, token)
    return no_store(render(request, "auth/reset.html", {}))


@router.post("/password/reset/{token}")
async def reset_password_form(
    request: Request,
    db: DbSession,
    token: str,
    new_password: FormField = "",
    new_password_confirm: FormField = "",
) -> Response:
    await get_valid_reset_token(db, token)  # a dead link says so, whatever was typed
    try:
        if new_password != new_password_confirm:
            raise ValidationFailed("Passwords do not match.")
        await reset_password(db, token, new_password)
    except ValidationFailed as exc:
        return no_store(render(request, "auth/reset.html", {"error": exc.message}, status_code=400))
    response = RedirectResponse("/login", status_code=303)
    flash(response, "Password updated. Sign in with your new password.", request.app.state.settings)
    return response


@router.post("/logout")
async def logout(
    db: DbSession, session: Annotated[UserSession | None, Depends(current_session)]
) -> Response:
    if session is not None:
        await revoke_session(db, session.id)
        await commit_and_publish(db, *sessions_revoked(session.user_id, [session.id]))
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


# require_user first: anonymous users go to the login page, and users who must change their
# password to the password form, before anything else.
@router.get("/")
async def home(
    request: Request, db: DbSession, user: Annotated[User, Depends(require_user)]
) -> Response:
    """The app's start page (the PWA opens here): the board this device showed last, or the
    user's only board, or else the board list."""
    raw = request.cookies.get(LAST_BOARD_COOKIE, "")
    last_opened = int(raw) if raw.isdecimal() and len(raw) <= 18 else None
    board_id = await landing_board_id(db, user, last_opened)
    return RedirectResponse("/boards" if board_id is None else f"/boards/{board_id}", 303)
