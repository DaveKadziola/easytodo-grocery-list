"""The board list and board pages, creating, renaming and archiving boards, and the "Archived
boards" page, where archived boards are restored or deleted for good."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.boards.service import (
    archive_board,
    count_archived_boards,
    create_board,
    delete_board,
    get_board_view,
    list_archived_boards,
    list_board_choices,
    list_boards,
    rename_board,
    restore_board,
)
from easytodo.errors import ValidationFailed
from easytodo.web import flash, is_htmx, prompt_answer, render

router = APIRouter(prefix="/boards")

LAST_BOARD_COOKIE = "easytodo_last_board"  # the board this device showed last (read by "/")
LAST_BOARD_MAX_AGE_S = 365 * 24 * 3600


@router.get("")
async def boards_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    context = {
        "boards": await list_boards(db, user),
        "archived_count": await count_archived_boards(db, user),
        "name": "",
    }
    return render(request, "boards/list.html", context)


@router.post("")
async def new_board(
    request: Request, db: DbSession, user: CurrentUser, name: FormField = ""
) -> Response:
    try:
        board = await create_board(db, user, name)
    except ValidationFailed as exc:
        context = {
            "boards": await list_boards(db, user),
            "archived_count": await count_archived_boards(db, user),
            "name": name,
            "error": exc.message,
        }
        return render(request, "boards/list.html", context, status_code=exc.status_code)
    return RedirectResponse(f"/boards/{board.id}", status_code=303)


# Before /{board_id}, which would take "archived" for a board id (and answer 404).
@router.get("/archived")
async def archived_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    context = {"archived": await list_archived_boards(db, user)}
    return render(request, "boards/archived.html", context)


@router.get("/{board_id}")
async def board_page(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    view = await get_board_view(db, board_id, user)  # first: a non-member gets the 404
    # The user's boards, for the board switcher in the phone's bottom bar and the dialog that
    # moves a category to another board: their names and roles, not the counts of the list.
    context = {"view": view, "boards": await list_board_choices(db, user)}
    response = render(request, "boards/board.html", context)
    # Where "/" (the app's start page) opens next time on this device.
    response.set_cookie(
        LAST_BOARD_COOKIE,
        str(view.board.id),
        max_age=LAST_BOARD_MAX_AGE_S,
        path="/",
        secure=request.app.state.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.patch("/{board_id}")
async def rename(
    request: Request, db: DbSession, user: CurrentUser, board_id: int, name: FormField = ""
) -> Response:
    await rename_board(db, board_id, user, name)
    if is_htmx(request):
        return Response(headers={"HX-Refresh": "true"})  # the new name shows up in several places
    return RedirectResponse("/boards", status_code=303)  # where the board is renamed from


@router.post("/{board_id}/archive")
async def archive(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    await archive_board(db, board_id, user)
    # To the list: refreshing the board's page would only find it gone.
    response: Response = (
        Response(headers={"HX-Redirect": "/boards"})
        if is_htmx(request)
        else RedirectResponse("/boards", status_code=303)
    )
    flash(response, "Board archived.", request.app.state.settings)
    return response


def _archive_page_again(request: Request, message: str) -> Response:
    """The "Archived boards" page again (reloaded in place for htmx), ``message`` as a toast."""
    response: Response = (
        Response(headers={"HX-Refresh": "true"})
        if is_htmx(request)
        else RedirectResponse("/boards/archived", status_code=303)
    )
    flash(response, message, request.app.state.settings)
    return response


@router.post("/{board_id}/restore")
async def restore(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    await restore_board(db, board_id, user)
    return _archive_page_again(request, "Board restored.")


@router.delete("/{board_id}")
async def delete(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    """Delete an archived board for good; the user typed its name into the ``hx-prompt``."""
    await delete_board(db, board_id, user, prompt_answer(request))
    return _archive_page_again(request, "Board deleted permanently.")
