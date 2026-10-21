"""The board list and board pages, and creating, renaming and deleting boards."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.boards.service import (
    create_board,
    delete_board,
    get_board_view,
    list_boards,
    rename_board,
)
from easytodo.errors import ValidationFailed
from easytodo.web import is_htmx, render

router = APIRouter(prefix="/boards")

LAST_BOARD_COOKIE = "easytodo_last_board"  # the board this device showed last (read by "/")
LAST_BOARD_MAX_AGE_S = 365 * 24 * 3600


@router.get("")
async def boards_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    context = {"boards": await list_boards(db, user), "name": ""}
    return render(request, "boards/list.html", context)


@router.post("")
async def new_board(
    request: Request, db: DbSession, user: CurrentUser, name: FormField = ""
) -> Response:
    try:
        board = await create_board(db, user, name)
    except ValidationFailed as exc:
        context = {"boards": await list_boards(db, user), "name": name, "error": exc.message}
        return render(request, "boards/list.html", context, status_code=exc.status_code)
    return RedirectResponse(f"/boards/{board.id}", status_code=303)


@router.get("/{board_id}")
async def board_page(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    view = await get_board_view(db, board_id, user)  # first: a non-member gets the 404
    # The user's boards, for the board switcher in the phone's bottom bar.
    context = {"view": view, "boards": await list_boards(db, user)}
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
    board = await rename_board(db, board_id, user, name)
    if is_htmx(request):
        return Response(headers={"HX-Refresh": "true"})  # the new name shows up in several places
    return RedirectResponse(f"/boards/{board.id}", status_code=303)


@router.delete("/{board_id}")
async def delete(request: Request, db: DbSession, user: CurrentUser, board_id: int) -> Response:
    await delete_board(db, board_id, user)
    if is_htmx(request):
        return Response(headers={"HX-Redirect": "/boards"})
    return RedirectResponse("/boards", status_code=303)
