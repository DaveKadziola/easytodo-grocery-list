"""The account pages: changing the password and managing the signed-in devices."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from easytodo.auth.deps import CurrentUser, DbSession, FormField, require_user
from easytodo.auth.sessions import revoke_all_sessions, revoke_session
from easytodo.errors import AppError, NotFound, ValidationFailed
from easytodo.models import UserSession
from easytodo.security.passwords import (
    MAX_PASSWORD_LENGTH,
    hash_password,
    validate_password,
    verify_password,
)
from easytodo.security.ratelimit import rate_limit
from easytodo.web import flash, render

router = APIRouter(prefix="/account")

_MAX_SESSION_ID = 2**63 - 1  # user_sessions.id is a BigInteger; a larger id cannot exist


@router.get("")
async def account_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    sessions = await db.scalars(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.expires_at > datetime.now(UTC))
        .order_by(UserSession.last_seen_at.desc(), UserSession.id.desc())
    )
    context = {"sessions": list(sessions), "current_session_id": request.state.session.id}
    return render(request, "account/index.html", context)


@router.get("/password", dependencies=[Depends(require_user)])
async def password_page(request: Request) -> Response:
    return render(request, "account/password.html", {})


# The limiter comes after require_user, so only signed-in requests use up the budget: a wrong
# "current password" is a password guess for whoever holds a stolen session.
@router.post("/password")
async def change_password(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: Annotated[None, Depends(rate_limit("password-change", 10, 900))],
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
    except AppError as exc:
        return render(request, "account/password.html", {"error": exc.message}, status_code=400)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # Commits the new hash together with the revocation.
    await revoke_all_sessions(db, user.id, except_session_id=request.state.session.id)
    response = RedirectResponse("/account", status_code=303)
    flash(response, "Password changed.", request.app.state.settings)
    return response


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, db: DbSession, user: CurrentUser) -> Response:
    await revoke_all_sessions(db, user.id, except_session_id=request.state.session.id)
    return RedirectResponse("/account", status_code=303)


@router.post("/sessions/{session_id}/revoke")
async def revoke_own_session(db: DbSession, user: CurrentUser, session_id: int) -> Response:
    # Someone else's session answers like one that does not exist.
    if not 0 < session_id <= _MAX_SESSION_ID:
        raise NotFound()
    if await db.scalar(select(UserSession.user_id).where(UserSession.id == session_id)) != user.id:
        raise NotFound()
    await revoke_session(db, session_id)
    return RedirectResponse("/account", status_code=303)
