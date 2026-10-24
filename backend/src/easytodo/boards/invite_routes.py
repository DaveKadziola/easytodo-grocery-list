"""Sharing a board: the members page with its invites, roles and removals, leaving a board, and
the invite page that a link or an e-mail leads to.

Every change on the members page answers with the whole page again; the page's htmx controls
swap in its ``#sharing`` part (``hx-select``).
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.boards.invites import (
    INVITE_TTL,
    ROLE_CHOICE,
    accept_invite,
    create_email_invite,
    create_link_invite,
    invite_url,
    list_pending_invites,
    preview_invite,
    revoke_invite,
)
from easytodo.boards.permissions import Action, can, require_board
from easytodo.boards.service import (
    change_role,
    is_member,
    leave_board,
    list_members,
    remove_member,
)
from easytodo.errors import ValidationFailed
from easytodo.models import BoardRole, User
from easytodo.security.ratelimit import limit_user
from easytodo.web import flash, is_htmx, no_store, render

router = APIRouter()

# Each e-mail invite sends a mail, and anyone who signs up owns a board, so one inviter may send
# at most this many per window (on any of their boards).
EMAIL_INVITE_LIMIT = 20
EMAIL_INVITE_WINDOW_S = 3600


def _role(value: str) -> BoardRole:
    try:
        return BoardRole(value)
    except ValueError:
        raise ValidationFailed(ROLE_CHOICE) from None


async def _members_page(
    request: Request,
    db: AsyncSession,
    user: User,
    board_id: int,
    status_code: int = 200,
    new_link: str | None = None,
    notice: str | None = None,
    link_email: str | None = None,
) -> Response:
    """The members page; ``new_link`` is a link invite just created (shown only this once), or
    the link of an e-mail invite to ``link_email`` whose mail was not delivered. A page that shows
    a link is never cached."""
    board, role = await require_board(db, board_id, user, Action.VIEW)
    manage = can(role, Action.MANAGE_MEMBERS)
    context = {
        "board": board,
        "role": role,
        "members": await list_members(db, board_id, user),
        "invites": await list_pending_invites(db, board_id, user) if manage else [],
        "new_link": new_link,
        "link_email": link_email,
        "valid_days": INVITE_TTL.days,
        "notice": notice,
    }
    response = render(request, "boards/members.html", context, status_code)
    return no_store(response) if new_link else response


@router.get("/boards/{board_id}/members")
async def members(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    return await _members_page(request, db, user, board_id)


@router.post("/boards/{board_id}/invites")
async def invite(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    board_id: int,
    role: FormField = "",
    email: FormField = "",
) -> Response:
    """An e-mail invite when ``email`` is given, otherwise a link to share."""
    if email.strip():
        limit_user(request, "invite-email", user.id, EMAIL_INVITE_LIMIT, EMAIL_INVITE_WINDOW_S)
        state = request.app.state
        sent = await create_email_invite(
            db, board_id, user, email, _role(role), state.mailer, state.settings
        )
        if sent.delivered:
            notice = f"Invite sent to {sent.invite.email}."
            return await _members_page(request, db, user, board_id, 201, notice=notice)
        # The console backend (the default) only logs mail, and SMTP may have failed: say so,
        # and show the link as for a link invite (only that address's account can use it).
        return await _members_page(
            request, db, user, board_id, 201, new_link=sent.link, link_email=sent.invite.email
        )
    raw_token, _ = await create_link_invite(db, board_id, user, _role(role))
    link = invite_url(request.app.state.settings, raw_token)
    return await _members_page(request, db, user, board_id, 201, new_link=link)


@router.delete("/boards/{board_id}/invites/{invite_id}")
async def revoke(
    request: Request, db: DbSession, user: CurrentUser, board_id: int, invite_id: int
) -> Response:
    await revoke_invite(db, board_id, invite_id, user)
    return await _members_page(request, db, user, board_id)


@router.patch("/boards/{board_id}/members/{user_id}")
async def set_role(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    board_id: int,
    user_id: int,
    role: FormField = "",
) -> Response:
    await change_role(db, board_id, user, user_id, _role(role))
    return await _members_page(request, db, user, board_id)


@router.delete("/boards/{board_id}/members/{user_id}")
async def remove(
    request: Request, db: DbSession, user: CurrentUser, board_id: int, user_id: int
) -> Response:
    await remove_member(db, board_id, user, user_id)
    return await _members_page(request, db, user, board_id)


@router.post("/boards/{board_id}/leave")
async def leave(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    await leave_board(db, board_id, user)
    if is_htmx(request):
        return Response(headers={"HX-Redirect": "/boards"})
    return RedirectResponse("/boards", status_code=303)


@router.get("/invite/{token}")
async def invite_page(request: Request, db: DbSession, user: CurrentUser, token: str) -> Response:
    preview = await preview_invite(db, token)
    if await is_member(db, preview.board_id, user):  # nothing to join: straight to the board
        response = RedirectResponse(f"/boards/{preview.board_id}", status_code=303)
        flash(response, "You're already a member of this board.", request.app.state.settings)
        return response
    # The token is in the page (the accept form's address): no cache keeps a copy.
    return no_store(render(request, "boards/invite.html", {"token": token, "preview": preview}))


@router.post("/invite/{token}/accept")
async def accept(db: DbSession, user: CurrentUser, token: str) -> Response:
    board = await accept_invite(db, token, user)
    return RedirectResponse(f"/boards/{board.id}", status_code=303)
