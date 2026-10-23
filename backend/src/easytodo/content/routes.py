"""Categories, items and checklist entries over HTMX, and the board's "Archived categories" page.
Each route answers with the HTML fragment to swap in: the whole board content, one category, an
item's details (also after a change to its checklist), or nothing (an archived category, a new
order); a category restored or deleted for good from the archive page reloads that page
instead."""

import json
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.attachments.routes import router as attachments_router
from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.boards.service import BoardView, get_board_view
from easytodo.content.checklist import (
    add_entry,
    check_entry,
    delete_entry,
    rename_entry,
    reorder_entries,
)
from easytodo.content.service import (
    archive_category,
    clear_done,
    create_category,
    create_item,
    delete_category,
    delete_item,
    get_category_view,
    get_item_details,
    list_archived_categories,
    move_item,
    rename_category,
    reorder_categories,
    restore_category,
    restore_item,
    toggle_item,
    update_item,
)
from easytodo.errors import ValidationFailed
from easytodo.models import User
from easytodo.web import flash, prompt_answer, render

router = APIRouter()

ITEM_DELETED = "Item deleted."
OptionalFormField = Annotated[str | None, Form()]


def _wanted_state(done: str | None) -> bool | None:
    """The state a tick asks for: "true" or "false" (the checkbox's new state, which board.js
    sends), or None when it sends none and the item or entry flips."""
    if done not in (None, "true", "false"):
        raise ValidationFailed("done must be true or false.")
    return None if done is None else done == "true"


def _board_content(request: Request, view: BoardView) -> Response:
    return render(request, "boards/_board_content.html", {"view": view})


async def _current_category(
    request: Request, db: AsyncSession, user: User, category_id: int, status_code: int = 200
) -> Response:
    """The category's fragment, with the controls ``user``'s role allows."""
    view, role = await get_category_view(db, category_id, user)
    context = {"category_view": view, "role": role}
    return render(request, "boards/_category.html", context, status_code)


async def _current_details(
    request: Request, db: AsyncSession, user: User, item_id: int, status_code: int = 200
) -> Response:
    """The item's details fragment (``#item-details``), without write controls for a viewer."""
    details = await get_item_details(db, item_id, user)
    return render(request, "boards/_item_details.html", {"details": details}, status_code)


@router.get("/boards/{board_id}/content")
async def board_content(
    request: Request, db: DbSession, user: CurrentUser, board_id: int
) -> Response:
    return _board_content(request, await get_board_view(db, board_id, user))


@router.get("/boards/{board_id}/archive")
async def archive_page(
    request: Request, db: DbSession, user: CurrentUser, board_id: int
) -> Response:
    board, role, archived = await list_archived_categories(db, board_id, user)
    context = {"board": board, "role": role, "archived": archived}
    return render(request, "boards/archive.html", context)


@router.post("/boards/{board_id}/categories")
async def new_category(
    request: Request, db: DbSession, user: CurrentUser, board_id: int, name: FormField = ""
) -> Response:
    category = await create_category(db, board_id, user, name)
    return await _current_category(request, db, user, category.id, status_code=201)


@router.put("/boards/{board_id}/categories/order")
async def reorder(
    db: DbSession,
    user: CurrentUser,
    board_id: int,
    ids: Annotated[list[int], Form(default_factory=list)],
) -> Response:
    await reorder_categories(db, board_id, user, ids)
    return Response(status_code=204)


@router.get("/categories/{category_id}")
async def category_fragment(
    request: Request, db: DbSession, user: CurrentUser, category_id: int
) -> Response:
    return await _current_category(request, db, user, category_id)


@router.patch("/categories/{category_id}")
async def rename(
    request: Request, db: DbSession, user: CurrentUser, category_id: int, name: FormField = ""
) -> Response:
    await rename_category(db, category_id, user, name)
    return await _current_category(request, db, user, category_id)


@router.post("/categories/{category_id}/archive")
async def archive(db: DbSession, user: CurrentUser, category_id: int) -> Response:
    await archive_category(db, category_id, user)
    # An empty 200: HTMX swaps the category out.
    toast = {"toast": {"level": "info", "message": "Category archived."}}
    return Response(headers={"HX-Trigger": json.dumps(toast)})


def _archive_page_again(request: Request, message: str) -> Response:
    """The "Archived categories" page again (reloaded in place), ``message`` as a toast."""
    response = Response(headers={"HX-Refresh": "true"})
    flash(response, message, request.app.state.settings)
    return response


@router.post("/categories/{category_id}/restore")
async def restore(request: Request, db: DbSession, user: CurrentUser, category_id: int) -> Response:
    category, renamed = await restore_category(db, category_id, user)
    message = f"Category restored as “{category.name}”." if renamed else "Category restored."
    return _archive_page_again(request, message)


@router.delete("/categories/{category_id}")
async def delete(request: Request, db: DbSession, user: CurrentUser, category_id: int) -> Response:
    """Delete an archived category for good; the user typed its name into the ``hx-prompt``."""
    await delete_category(db, category_id, user, prompt_answer(request))
    return _archive_page_again(request, "Category deleted permanently.")


@router.post("/categories/{category_id}/items")
async def new_item(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    category_id: int,
    name: FormField = "",
    description: OptionalFormField = None,
) -> Response:
    await create_item(db, category_id, user, name, description)
    return await _current_category(request, db, user, category_id)


@router.post("/categories/{category_id}/clear-done")
async def clear_done_items(
    request: Request, db: DbSession, user: CurrentUser, category_id: int
) -> Response:
    await clear_done(db, category_id, user)
    return await _current_category(request, db, user, category_id)


@router.get("/items/{item_id}/details")
async def item_details(
    request: Request, db: DbSession, user: CurrentUser, item_id: int
) -> Response:
    """The details of an item for any member of its board: who added it, and when it was added
    and last modified. A page with the item open fetches them again when an event names it."""
    return await _current_details(request, db, user, item_id)


@router.post("/items/{item_id}/checklist")
async def new_entry(
    request: Request, db: DbSession, user: CurrentUser, item_id: int, text: FormField = ""
) -> Response:
    await add_entry(db, item_id, user, text)
    return await _current_details(request, db, user, item_id)


@router.put("/items/{item_id}/checklist/order")
async def reorder_checklist(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    item_id: int,
    ids: Annotated[list[int], Form(default_factory=list)],
) -> Response:
    await reorder_entries(db, item_id, user, ids)
    return await _current_details(request, db, user, item_id)


@router.patch("/checklist/{entry_id}")
async def edit_entry(
    request: Request, db: DbSession, user: CurrentUser, entry_id: int, text: FormField = ""
) -> Response:
    entry = await rename_entry(db, entry_id, user, text)
    return await _current_details(request, db, user, entry.item_id)


@router.post("/checklist/{entry_id}/check")
async def check(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    entry_id: int,
    done: OptionalFormField = None,
) -> Response:
    """Like ``toggle``: ``done`` ("true" or "false") sets the state, without it the entry flips."""
    entry = await check_entry(db, entry_id, user, _wanted_state(done))
    return await _current_details(request, db, user, entry.item_id)


@router.delete("/checklist/{entry_id}")
async def remove_entry(
    request: Request, db: DbSession, user: CurrentUser, entry_id: int
) -> Response:
    item_id = await delete_entry(db, entry_id, user)
    return await _current_details(request, db, user, item_id)


@router.patch("/items/{item_id}")
async def edit_item(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    item_id: int,
    name: FormField = "",
    description: OptionalFormField = None,
) -> Response:
    item = await update_item(db, item_id, user, name=name, description=description)
    return await _current_category(request, db, user, item.category_id)


@router.post("/items/{item_id}/toggle")
async def toggle(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    item_id: int,
    done: OptionalFormField = None,
) -> Response:
    """``done`` ("true" or "false": the checkbox's new state, which board.js sends) sets the
    state; without it the item flips. (A str, not a bool: FastAPI reads a missing bool form
    field as False, the way an unticked HTML checkbox is left out.)"""
    item = await toggle_item(db, item_id, user, _wanted_state(done))
    return await _current_category(request, db, user, item.category_id)


@router.post("/items/{item_id}/move")
async def move(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    item_id: int,
    category_id: Annotated[int, Form()],
) -> Response:
    item, _ = await move_item(db, item_id, user, category_id)
    return _board_content(request, await get_board_view(db, item.board_id, user))


@router.delete("/items/{item_id}")
async def remove_item(request: Request, db: DbSession, user: CurrentUser, item_id: int) -> Response:
    """Delete the item (see ``delete_item``); the toast offers "Undo", which app.js answers with a
    POST to ``undo.url``, swapped into ``undo.swap_into``."""
    category_id = await delete_item(db, item_id, user)
    response = await _current_category(request, db, user, category_id)
    undo = {"url": f"/items/{item_id}/restore", "swap_into": f"#category-{category_id}"}
    toast = {"toast": {"level": "info", "message": ITEM_DELETED, "undo": undo}}
    response.headers["HX-Trigger"] = json.dumps(toast)
    return response


@router.post("/items/{item_id}/restore")
async def restore_deleted_item(
    request: Request, db: DbSession, user: CurrentUser, item_id: int
) -> Response:
    """The "Undo" of a delete."""
    item = await restore_item(db, item_id, user)
    return await _current_category(request, db, user, item.category_id)


# The attachments' routes belong to the item's details; they are in their own module.
router.include_router(attachments_router)
