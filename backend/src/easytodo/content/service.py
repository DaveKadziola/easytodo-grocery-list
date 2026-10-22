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

from easytodo.boards.permissions import (
    BOARD_GONE,
    Action,
    RowLock,
    is_possible_id,
    load_for_member,
    require_board,
)
from easytodo.boards.service import ITEM_ORDER, CategoryView
from easytodo.content.validation import (
    CATEGORY_NAME_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
    clean_description,
    clean_name,
)
from easytodo.errors import Conflict, NotFound, ValidationFailed
from easytodo.models import Board, BoardMember, BoardRole, Category, Item, User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

CATEGORY_GONE = "This category no longer exists."
ITEM_GONE = "This item no longer exists."
_UNIQUE_VIOLATION = "23505"  # PostgreSQL SQLSTATEs
_FOREIGN_KEY_VIOLATION = "23503"


async def _load[M: (Category, Item)](
    db: AsyncSession,
    model: type[M],
    object_id: int,
    user: User,
    action: Action,
    gone: str,
    lock: RowLock,
) -> tuple[M, BoardRole]:
    """The row and ``user``'s role on its board, in one query joined with ``board_members`` and
    the board, which must not be archived.

    For a change (any action but VIEW) the row stays locked in mode ``lock`` until the commit: a
    delete from another device that got there first makes this ``NotFound``, a later one waits
    for it. A member whose role does not allow the change takes no lock (see
    ``load_for_member``).
    """
    if not is_possible_id(object_id):
        raise NotFound(gone)
    query = (
        select(model, BoardMember.role)
        .join(Board, Board.id == model.board_id)
        .join(BoardMember, BoardMember.board_id == model.board_id)
        .where(model.id == object_id, BoardMember.user_id == user.id, Board.archived_at.is_(None))
    )
    row_lock = None if action is Action.VIEW else lock
    found, role = await load_for_member(db, query, action, gone, lock=row_lock, of=model)
    return found, role


async def load_category(
    db: AsyncSession,
    category_id: int,
    user: User,
    action: Action,
    *,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
) -> tuple[Category, BoardRole]:
    """The category and ``user``'s role; for a change, locked in mode ``lock`` (see ``_load``)."""
    return await _load(db, Category, category_id, user, action, CATEGORY_GONE, lock)


async def load_item(
    db: AsyncSession,
    item_id: int,
    user: User,
    action: Action,
    *,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
) -> tuple[Item, BoardRole]:
    """The item and ``user``'s role; for a change, locked in mode ``lock`` (see ``_load``)."""
    return await _load(db, Item, item_id, user, action, ITEM_GONE, lock)


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
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == _UNIQUE_VIOLATION:
            raise Conflict("A category with this name already exists.") from None
        if sqlstate == _FOREIGN_KEY_VIOLATION:  # the board was deleted meanwhile
            raise NotFound(BOARD_GONE) from None
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
    category, _ = await load_category(
        db, category_id, user, Action.EDIT_CONTENT, lock=RowLock.UPDATE
    )
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
    categories = await db.scalars(  # locked in id order: two reorders at once cannot deadlock
        select(Category)
        .where(Category.board_id == board.id)
        .order_by(Category.id)
        .with_for_update()
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
    # A share lock: items can be added to the category at the same time, while a delete waits.
    category, _ = await load_category(
        db, category_id, user, Action.EDIT_CONTENT, lock=RowLock.SHARE
    )
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


async def toggle_item(db: AsyncSession, item_id: int, user: User, done: bool | None = None) -> Item:
    """Mark the item done (now) or open again: as ``done`` says, or the other way round from
    what it is when ``done`` is None.

    Clients send the state their user wants, so two devices ticking the same item (one of them
    from a view that is not up to date yet) both leave it done instead of cancelling each other
    out. When the item is already in that state nothing changes and no event is sent.
    """
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    wanted = not item.is_done if done is None else done
    if wanted == item.is_done:
        await commit_and_publish(db)  # nothing to change: just ends the transaction (row lock)
        return item
    item.is_done = wanted
    item.done_at = datetime.now(UTC) if wanted else None
    await commit_and_publish(db, _category_changed(item.board_id, item.category_id))
    return item


async def move_item(
    db: AsyncSession, item_id: int, user: User, target_category_id: int
) -> tuple[Item, int]:
    """Move the item to another category of its own board; returns it and its old category id."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    if target_category_id == item.category_id:
        # Already there: nothing to change, and no lock on its category, which could deadlock
        # against a delete of that category that holds it and waits for the item.
        await commit_and_publish(db)
        return item, item.category_id
    target = None
    if is_possible_id(target_category_id):
        target = await db.scalar(  # a share lock: other items can move in at the same time
            select(Category)
            .where(Category.id == target_category_id, Category.board_id == item.board_id)
            .with_for_update(read=True)
        )
    if target is None:  # also a category on another board: items never change boards
        raise NotFound(CATEGORY_GONE)
    old_category_id = item.category_id
    item.category_id = target.id
    await commit_and_publish(db, _category_changed(item.board_id, old_category_id, target.id))
    return item, old_category_id


async def delete_item(db: AsyncSession, item_id: int, user: User) -> int:
    """Delete the item; returns the id of the category it was in."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT, lock=RowLock.UPDATE)
    event = _category_changed(item.board_id, item.category_id)
    await db.delete(item)
    await commit_and_publish(db, event)
    return item.category_id


async def clear_done(db: AsyncSession, category_id: int, user: User) -> int:
    """Delete the category's done items; returns how many there were."""
    category, _ = await load_category(
        db, category_id, user, Action.EDIT_CONTENT, lock=RowLock.SHARE
    )
    result = await db.execute(
        delete(Item).where(Item.category_id == category.id, Item.is_done.is_(True))
    )
    removed = result.rowcount
    events = [_category_changed(category.board_id, category.id)] if removed else []
    await commit_and_publish(db, *events)
    return removed
