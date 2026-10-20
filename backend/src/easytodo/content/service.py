"""Categories and items.

Every function finds the category or item through the user's own board memberships, so one on
a board the user is not a member of answers like one that does not exist (``NotFound``); a
member whose role does not allow the change gets ``Forbidden``. Each change is committed
together with the board event it causes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.permissions import Action, check_permission, is_possible_id, require_board
from easytodo.boards.service import ITEM_ORDER, CategoryView
from easytodo.content.validation import (
    CATEGORY_NAME_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
    clean_description,
    clean_name,
)
from easytodo.errors import Conflict, NotFound, ValidationFailed
from easytodo.models import BoardMember, BoardRole, Category, Item, User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

CATEGORY_GONE = "This category no longer exists."
ITEM_GONE = "This item no longer exists."
_UNIQUE_VIOLATION = "23505"  # PostgreSQL SQLSTATE


async def _load[M: (Category, Item)](
    db: AsyncSession, model: type[M], object_id: int, user: User, action: Action, gone: str
) -> tuple[M, BoardRole]:
    """The row and ``user``'s role on its board, in one query joined with ``board_members``.

    For a change (any action but VIEW) the row stays locked until the commit: a delete from
    another device that got there first makes this ``NotFound``, a later one waits for it.
    """
    row = None
    if is_possible_id(object_id):
        query = (
            select(model, BoardMember.role)
            .join(BoardMember, BoardMember.board_id == model.board_id)
            .where(model.id == object_id, BoardMember.user_id == user.id)
        )
        if action is not Action.VIEW:
            query = query.with_for_update(of=model)
        row = (await db.execute(query)).one_or_none()
    if row is None:
        raise NotFound(gone)
    found, role = row
    check_permission(role, action)
    return found, role


async def load_category(
    db: AsyncSession, category_id: int, user: User, action: Action
) -> tuple[Category, BoardRole]:
    return await _load(db, Category, category_id, user, action, CATEGORY_GONE)


async def load_item(
    db: AsyncSession, item_id: int, user: User, action: Action
) -> tuple[Item, BoardRole]:
    return await _load(db, Item, item_id, user, action, ITEM_GONE)


def _category_changed(board_id: int, *category_ids: int) -> BoardEvent:
    return BoardEvent(
        EventType.CATEGORY_CHANGED,
        board_id=board_id,
        category_ids=tuple(dict.fromkeys(category_ids)),
    )


def _clean_category_name(name: str) -> str:
    return clean_name(name, max_len=CATEGORY_NAME_MAX_LENGTH, label="Category name")


def _clean_item_name(name: str) -> str:
    return clean_name(name, max_len=ITEM_NAME_MAX_LENGTH, label="Item name")


@asynccontextmanager
async def _unique_category_name(db: AsyncSession) -> AsyncIterator[None]:
    """Write the new or renamed category of the block; a name already on the board (ignoring
    case) is a ``Conflict``.

    A savepoint keeps the failed write from rolling back, and so expiring, everything else the
    request loaded (such as the signed-in user, which the error page shows).
    """
    try:
        async with db.begin_nested():
            yield
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise Conflict("A category with this name already exists.") from None
        raise


async def create_category(db: AsyncSession, board_id: int, user: User, name: str) -> Category:
    """A new category at the end of the board."""
    board, _ = await require_board(db, board_id, user, Action.EDIT_CONTENT)
    name = _clean_category_name(name)
    last = await db.scalar(select(func.max(Category.position)).where(Category.board_id == board.id))
    category = Category(board_id=board.id, name=name, position=0 if last is None else last + 1)
    async with _unique_category_name(db):
        db.add(category)
    await commit_and_publish(db, _category_changed(board.id, category.id))
    return category


async def rename_category(db: AsyncSession, category_id: int, user: User, name: str) -> Category:
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    name = _clean_category_name(name)
    async with _unique_category_name(db):
        category.name = name
    await commit_and_publish(db, _category_changed(category.board_id, category.id))
    return category


async def delete_category(db: AsyncSession, category_id: int, user: User) -> None:
    """Delete the category; its items go with it (FK cascade)."""
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    event = BoardEvent(
        EventType.CATEGORY_DELETED, board_id=category.board_id, category_ids=(category.id,)
    )
    await db.delete(category)
    await commit_and_publish(db, event)


async def reorder_categories(
    db: AsyncSession, board_id: int, user: User, ordered_ids: list[int]
) -> None:
    """Put the board's categories in the order of ``ordered_ids``, which must name each of them
    exactly once (a client that missed a change gets ``ValidationFailed``)."""
    board, _ = await require_board(db, board_id, user, Action.EDIT_CONTENT)
    categories = await db.scalars(
        select(Category).where(Category.board_id == board.id).with_for_update()
    )
    by_id = {category.id: category for category in categories}
    if sorted(ordered_ids) != sorted(by_id):
        raise ValidationFailed("Category order is out of date. Refresh the board.")
    for position, category_id in enumerate(ordered_ids):
        by_id[category_id].position = position
    await commit_and_publish(db, BoardEvent(EventType.CATEGORIES_REORDERED, board_id=board.id))


async def get_category_view(
    db: AsyncSession, category_id: int, user: User
) -> tuple[CategoryView, BoardRole]:
    """The category with its items (open first, then by name) and ``user``'s role."""
    category, role = await load_category(db, category_id, user, Action.VIEW)
    items = await db.scalars(
        select(Item).where(Item.category_id == category.id).order_by(*ITEM_ORDER)
    )
    return CategoryView(category, list(items)), role


async def create_item(
    db: AsyncSession, category_id: int, user: User, name: str, description: str | None = None
) -> Item:
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    item = Item(
        board_id=category.board_id,  # always the category's board, never taken from the request
        category_id=category.id,
        name=_clean_item_name(name),
        description=clean_description(description),
        is_done=False,
    )
    db.add(item)
    await commit_and_publish(db, _category_changed(category.board_id, category.id))
    return item


async def update_item(
    db: AsyncSession, item_id: int, user: User, *, name: str, description: str | None
) -> Item:
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    item.name = _clean_item_name(name)
    item.description = clean_description(description)
    await commit_and_publish(db, _category_changed(item.board_id, item.category_id))
    return item


async def toggle_item(db: AsyncSession, item_id: int, user: User) -> Item:
    """Mark the item done (now) or open again."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    item.is_done = not item.is_done
    item.done_at = datetime.now(UTC) if item.is_done else None
    await commit_and_publish(db, _category_changed(item.board_id, item.category_id))
    return item


async def move_item(
    db: AsyncSession, item_id: int, user: User, target_category_id: int
) -> tuple[Item, int]:
    """Move the item to another category of its own board; returns it and its old category id."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    target = None
    if is_possible_id(target_category_id):
        target = await db.scalar(
            select(Category)
            .where(Category.id == target_category_id, Category.board_id == item.board_id)
            .with_for_update()
        )
    if target is None:  # also a category on another board: items never change boards
        raise NotFound(CATEGORY_GONE)
    old_category_id = item.category_id
    item.category_id = target.id
    await commit_and_publish(db, _category_changed(item.board_id, old_category_id, target.id))
    return item, old_category_id


async def delete_item(db: AsyncSession, item_id: int, user: User) -> int:
    """Delete the item; returns the id of the category it was in."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    event = _category_changed(item.board_id, item.category_id)
    await db.delete(item)
    await commit_and_publish(db, event)
    return item.category_id


async def clear_done(db: AsyncSession, category_id: int, user: User) -> int:
    """Delete the category's done items; returns how many there were."""
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    result = await db.execute(
        delete(Item).where(Item.category_id == category.id, Item.is_done.is_(True))
    )
    removed = result.rowcount
    events = [_category_changed(category.board_id, category.id)] if removed else []
    await commit_and_publish(db, *events)
    return removed
