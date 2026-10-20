"""Categories and items over HTMX. Each route answers with the HTML fragment to swap in: the
whole board content, one category, or nothing (a deleted category, a new order)."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.boards.service import BoardView, CategoryView, get_board_view
from easytodo.content.service import (
    clear_done,
    create_category,
    create_item,
    delete_category,
    delete_item,
    get_category_view,
    move_item,
    rename_category,
    reorder_categories,
    toggle_item,
    update_item,
)
from easytodo.models import User
from easytodo.web import render

router = APIRouter()

OptionalFormField = Annotated[str | None, Form()]


def _board_content(request: Request, view: BoardView) -> Response:
    return render(request, "boards/_board_content.html", {"view": view})


def _category(request: Request, view: CategoryView, status_code: int = 200) -> Response:
    return render(request, "boards/_category.html", {"category_view": view}, status_code)


async def _current_category(
    request: Request, db: AsyncSession, user: User, category_id: int
) -> Response:
    view, _ = await get_category_view(db, category_id, user)
    return _category(request, view)


@router.get("/boards/{board_id}/content")
async def board_content(
    request: Request, db: DbSession, user: CurrentUser, board_id: int
) -> Response:
    return _board_content(request, await get_board_view(db, board_id, user))


@router.post("/boards/{board_id}/categories")
async def new_category(
    request: Request, db: DbSession, user: CurrentUser, board_id: int, name: FormField = ""
) -> Response:
    category = await create_category(db, board_id, user, name)
    return _category(request, CategoryView(category, []), status_code=201)


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


@router.delete("/categories/{category_id}")
async def delete(db: DbSession, user: CurrentUser, category_id: int) -> Response:
    await delete_category(db, category_id, user)
    return Response()  # an empty 200: HTMX swaps the category out


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
async def toggle(request: Request, db: DbSession, user: CurrentUser, item_id: int) -> Response:
    item = await toggle_item(db, item_id, user)
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
    category_id = await delete_item(db, item_id, user)
    return await _current_category(request, db, user, category_id)
