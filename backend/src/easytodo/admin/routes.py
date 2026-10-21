"""The admin panel under ``/admin``: the statistics, the user list and its actions, and creating
an account with a temporary password. Only admins reach it; anyone else gets a 404, so it does
not show that it exists.

The actions in the user list are htmx requests: each answers with the user's row again (swapped
in place; nothing once the account is deleted) and a toast. Without htmx they go back to the list.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.admin.service import (
    create_user_with_temp_password,
    delete_user,
    get_stats,
    get_user,
    get_user_row,
    list_users,
    reset_2fa,
    set_status,
)
from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.auth.reset import request_password_reset
from easytodo.errors import Conflict, NotFound, ValidationFailed
from easytodo.models import User, UserStatus
from easytodo.web import flash, is_htmx, render

USERS_PATH = "/admin/users"
ACCOUNT_DISABLED = "This account is disabled."


async def require_admin(user: CurrentUser) -> User:
    """The signed-in user, if an admin; anyone else gets ``NotFound``."""
    if not user.is_admin:
        raise NotFound()
    return user


AdminUser = Annotated[User, Depends(require_admin)]

# On the router, so no route under /admin can do without it.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("")
async def stats_page(request: Request, db: DbSession) -> Response:
    return render(request, "admin/index.html", {"stats": await get_stats(db)})


@router.get("/users")
async def users_page(request: Request, db: DbSession, q: str = "", page: int = 1) -> Response:
    q = q.strip()
    context = {"page": await list_users(db, q=q or None, page=page), "q": q}
    return render(request, "admin/users.html", context)


@router.post("/users")
async def new_user(
    request: Request,
    db: DbSession,
    email: FormField = "",
    is_admin: Annotated[bool, Form()] = False,
) -> Response:
    try:
        user, password = await create_user_with_temp_password(db, email, is_admin=is_admin)
    except (ValidationFailed, Conflict) as exc:
        context = {
            "page": await list_users(db, q=None, page=1),
            "q": "",
            "email": email,
            "is_admin": is_admin,
            "error": exc.message,
        }
        return render(request, "admin/users.html", context, status_code=exc.status_code)
    response = render(request, "admin/user_created.html", {"user": user, "password": password})
    response.headers["Cache-Control"] = "no-store"  # the password is on this page only
    return response


async def _answer(
    request: Request, db: AsyncSession, message: str, user_id: int | None
) -> Response:
    """What an action in the user list answers: for htmx, the row of ``user_id`` (an empty body
    for ``None``: the row goes) and ``message`` as a toast; otherwise the user list, where
    ``message`` shows as a toast."""
    if not is_htmx(request):
        response = RedirectResponse(USERS_PATH, status_code=303)
        flash(response, message, request.app.state.settings)
        return response
    if user_id is None:
        response = Response()
    else:
        response = render(request, "admin/_user_row.html", {"row": await get_user_row(db, user_id)})
    response.headers["HX-Trigger"] = json.dumps({"toast": {"level": "info", "message": message}})
    return response


@router.delete("/users/{user_id}")
async def remove_user(request: Request, db: DbSession, admin: AdminUser, user_id: int) -> Response:
    await delete_user(db, admin, user_id)
    return await _answer(request, db, "Account deleted.", None)


@router.post("/users/{user_id}/send-reset")
async def send_reset(request: Request, db: DbSession, user_id: int) -> Response:
    user = await get_user(db, user_id)
    if user.status == UserStatus.DISABLED:  # it would get no mail
        raise Conflict(ACCOUNT_DISABLED)
    state = request.app.state
    await request_password_reset(db, user.email, state.mailer, state.settings)
    return await _answer(request, db, f"Password reset link sent to {user.email}.", user.id)


@router.post("/users/{user_id}/status")
async def change_status(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    user_id: int,
    status: Annotated[UserStatus, Form()],
) -> Response:
    await set_status(db, admin, user_id, status)
    if status == UserStatus.DISABLED:
        message = "Account disabled. It is signed out everywhere."
    else:
        message = "Account enabled."
    return await _answer(request, db, message, user_id)


@router.post("/users/{user_id}/reset-2fa")
async def reset_two_factor(
    request: Request, db: DbSession, admin: AdminUser, user_id: int
) -> Response:
    await reset_2fa(db, admin, user_id)
    return await _answer(request, db, "Two-factor authentication is off for this account.", user_id)
