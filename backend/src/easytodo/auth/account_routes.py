"""The account pages: the profile and settings, changing the password, two-factor authentication
and managing the signed-in devices."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.deps import CurrentUser, DbSession, FormField, require_user
from easytodo.auth.reset import invalidate_reset_tokens
from easytodo.auth.sessions import revoke_all_sessions, revoke_session, sessions_revoked
from easytodo.auth.twofa import (
    ALREADY_ON,
    begin_setup,
    check_password,
    confirm_setup,
    disable_2fa,
    regenerate_recovery_codes,
)
from easytodo.content.validation import clean_name
from easytodo.errors import AppError, Conflict, NotFound, ValidationFailed
from easytodo.models import RETENTION_CHOICES, SORT_FIELDS, User, UserSession
from easytodo.realtime.events import commit_and_publish
from easytodo.security.passwords import (
    MAX_PASSWORD_LENGTH,
    hash_password,
    validate_password,
    verify_password,
)
from easytodo.security.ratelimit import rate_limit
from easytodo.security.totp import provisioning_uri, qr_svg
from easytodo.web import flash, render

router = APIRouter(prefix="/account")

_MAX_SESSION_ID = 2**63 - 1  # user_sessions.id is a BigInteger; a larger id cannot exist

# Every form that checks the current password shares this budget. The limiter comes after
# require_user, so only signed-in requests use it up: a wrong password is a password guess for
# whoever holds a stolen session.
PasswordGuesses = Annotated[None, Depends(rate_limit("password-change", 10, 900))]

DISPLAY_NAME_MAX_LENGTH = 60  # users.display_name is a String(60)
PROFILE_SAVED = "Profile saved."
ARCHIVE_SAVED = "Archive settings saved."
SORT_SAVED = "Sorting saved."
RETENTION_INVALID = "Choose how long archived items are kept."
SORT_INVALID = "Choose a valid sort order."

# The archive form's "days" -> users.archive_retention_days; None (NULL) keeps archived items.
_RETENTION_BY_FORM_VALUE = {str(days): days for days in RETENTION_CHOICES} | {"never": None}
_SORT_DIRECTIONS = ("asc", "desc")  # the sort form's "sort_direction"


async def _account_page(
    request: Request,
    db: AsyncSession,
    user: User,
    context: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    sessions = await db.scalars(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.expires_at > datetime.now(UTC))
        .order_by(UserSession.last_seen_at.desc(), UserSession.id.desc())
    )
    context = {
        "sessions": list(sessions),
        "current_session_id": request.state.session.id,
        "retention_choices": RETENTION_CHOICES,
    } | context
    return render(request, "account/index.html", context, status_code=status_code)


def _no_store(response: HTMLResponse) -> HTMLResponse:
    """For a page with a TOTP secret or recovery codes: no copy is kept in any cache."""
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("")
async def account_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    return await _account_page(request, db, user, {})


@router.get("/password", dependencies=[Depends(require_user)])
async def password_page(request: Request) -> Response:
    return render(request, "account/password.html", {})


@router.post("/password")
async def change_password(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: PasswordGuesses,
    current_password: FormField = "",
    new_password: FormField = "",
    new_password_confirm: FormField = "",
) -> Response:
    try:
        if len(current_password) > MAX_PASSWORD_LENGTH or not verify_password(
            user.password_hash, current_password
        ):
            raise ValidationFailed("Current password is incorrect.")
        if new_password != new_password_confirm:
            raise ValidationFailed("Passwords do not match.")
        validate_password(new_password)
        # A forced change replaces a password someone else knows (an admin's temporary one).
        if user.must_change_password and verify_password(user.password_hash, new_password):
            raise ValidationFailed("Choose a new password, not the temporary one.")
    except AppError as exc:
        return render(request, "account/password.html", {"error": exc.message}, status_code=400)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await invalidate_reset_tokens(db, user.id)  # a reset link in the mailbox would override it
    revoked = await revoke_all_sessions(db, user.id, except_session_id=request.state.session.id)
    await commit_and_publish(db, *sessions_revoked(user.id, revoked))  # with the new hash
    response = RedirectResponse("/account", status_code=303)
    flash(response, "Password changed.", request.app.state.settings)
    return response


def _setup_page(
    request: Request, user: User, secret: str, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    # segno's SVG holds only the QR code's paths (the URI is encoded in them), so it goes in as is.
    qr_code = Markup(qr_svg(provisioning_uri(secret, user.email)))
    context = {"secret": secret, "qr_code": qr_code, "error": error}
    return _no_store(render(request, "account/2fa_setup.html", context, status_code=status_code))


@router.get("/2fa/setup")
async def two_factor_setup_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    """The QR code to scan, and the key to type in instead: the secret of an earlier visit while
    it is not confirmed yet, so a reload does not undo a scan."""
    return _setup_page(request, user, await begin_setup(db, user, request.app.state.settings))


@router.post("/2fa/setup")
async def confirm_two_factor_setup(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: PasswordGuesses,
    password: FormField = "",
    code: FormField = "",
) -> Response:
    """Turn 2FA on: needs the current password as well as a code, because a stolen session that
    turns it on would lock the owner out."""
    if user.totp_enabled:  # before the password check, whose error page shows the secret
        raise Conflict(ALREADY_ON)
    settings = request.app.state.settings
    try:
        check_password(user, password)
        codes = await confirm_setup(db, user, code, settings)
    except ValidationFailed as exc:
        # The secret already scanned stays, so another try needs no new scan (a new one only
        # when there is none, or it cannot be decrypted since EASYTODO_SECRET_KEY changed).
        secret = await begin_setup(db, user, settings)
        return _setup_page(request, user, secret, error=exc.message, status_code=400)
    return _no_store(
        render(
            request,
            "account/2fa_recovery.html",
            {"codes": codes, "generated_at": datetime.now(UTC)},
        )
    )


@router.post("/2fa/disable")
async def disable_two_factor(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: PasswordGuesses,
    password: FormField = "",
    code: FormField = "",
) -> Response:
    settings = request.app.state.settings
    try:
        await disable_2fa(db, user, password, code, settings)
    except ValidationFailed as exc:
        return await _account_page(request, db, user, {"twofa_error": exc.message}, 400)
    response = RedirectResponse("/account", status_code=303)
    flash(response, "Two-factor authentication is off.", settings)
    return response


@router.post("/2fa/recovery-codes")
async def new_recovery_codes(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: PasswordGuesses,
    password: FormField = "",
) -> Response:
    try:
        codes = await regenerate_recovery_codes(db, user, password)
    except ValidationFailed as exc:
        return await _account_page(request, db, user, {"twofa_error": exc.message}, 400)
    return _no_store(
        render(
            request,
            "account/2fa_recovery.html",
            {"codes": codes, "generated_at": datetime.now(UTC)},
        )
    )


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, db: DbSession, user: CurrentUser) -> Response:
    revoked = await revoke_all_sessions(db, user.id, except_session_id=request.state.session.id)
    await commit_and_publish(db, *sessions_revoked(user.id, revoked))
    return RedirectResponse("/account", status_code=303)


@router.post("/sessions/{session_id}/revoke")
async def revoke_own_session(db: DbSession, user: CurrentUser, session_id: int) -> Response:
    # Someone else's session answers like one that does not exist.
    if not 0 < session_id <= _MAX_SESSION_ID:
        raise NotFound()
    if await db.scalar(select(UserSession.user_id).where(UserSession.id == session_id)) != user.id:
        raise NotFound()
    await revoke_session(db, session_id)
    await commit_and_publish(db, *sessions_revoked(user.id, [session_id]))
    return RedirectResponse("/account", status_code=303)


def _saved(request: Request, message: str) -> Response:
    response = RedirectResponse("/account", status_code=303)
    flash(response, message, request.app.state.settings)
    return response


# The three settings forms below check all of their fields before they change any. They do not
# publish an event (nobody else's page shows these) and are not password guesses.
@router.post("/profile")
async def save_profile(
    request: Request, db: DbSession, user: CurrentUser, display_name: FormField = ""
) -> Response:
    try:
        # Empty is not an error here: it takes the name away, and the email's part shows instead.
        name = (
            clean_name(display_name, max_len=DISPLAY_NAME_MAX_LENGTH, label="Display name")
            if display_name.strip()
            else None
        )
    except ValidationFailed as exc:
        return await _account_page(request, db, user, {"profile_error": exc.message}, 400)
    user.display_name = name
    await db.commit()
    return _saved(request, PROFILE_SAVED)


@router.post("/archive-retention")
async def save_archive_retention(
    request: Request, db: DbSession, user: CurrentUser, days: FormField = ""
) -> Response:
    if days not in _RETENTION_BY_FORM_VALUE:
        return await _account_page(request, db, user, {"archive_error": RETENTION_INVALID}, 400)
    user.archive_retention_days = _RETENTION_BY_FORM_VALUE[days]  # "never": NULL
    await db.commit()
    return _saved(request, ARCHIVE_SAVED)


@router.post("/sort")
async def save_sort(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    sort_field: FormField = "",
    sort_direction: FormField = "",
    sort_open_first: FormField = "",  # a checkbox: "true" when ticked, absent when not
) -> Response:
    if (
        sort_field not in SORT_FIELDS
        or sort_direction not in _SORT_DIRECTIONS
        or sort_open_first not in ("", "true")
    ):
        return await _account_page(request, db, user, {"sort_error": SORT_INVALID}, 400)
    user.sort_field = sort_field
    user.sort_descending = sort_direction == "desc"
    user.sort_open_first = sort_open_first == "true"
    await db.commit()
    return _saved(request, SORT_SAVED)
