"""Categories and items, and the board's archive of categories.

Every function finds the category or item through the user's own board memberships, so one on
a board the user is not a member of answers like one that does not exist (``NotFound``); a
member whose role does not allow the change gets ``Forbidden``. Each change is committed
together with the board event it causes.

An archived category is gone from its board, with its items, for every member (each lookup
leaves it out), but keeps its data until it is restored or deleted for good from the board's
archive; meanwhile its name is free for a new category.

Deleting an item only marks it (``deleted_at``): it is gone from every page and lookup at once,
but its row, author, checklist and attachments stay for ``UNDO_WINDOW``, in which "Undo" brings it
back as it was. Every read of items leaves the marked ones out.
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

from sqlalchemy import BigInteger, ColumnElement, Select, bindparam, delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.permissions import (
    BOARD_GONE,
    Action,
    MemberLookup,
    RowLock,
    check_permission,
    is_possible_id,
    load_for_member,
    member_lookup,
    require_board,
)
from easytodo.boards.service import (
    Author,
    CategoryView,
    select_item_views,
    select_member_category,
    sort_choice,
)
from easytodo.content.validation import (
    CATEGORY_NAME_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
    clean_description,
    clean_name,
)
from easytodo.display import display_name, user_display_name
from easytodo.errors import Conflict, NameTaken, NotFound, ValidationFailed
from easytodo.models import (
    Attachment,
    Board,
    BoardMember,
    BoardRole,
    Category,
    ChecklistEntry,
    Item,
    NotificationKind,
    User,
)
from easytodo.notifications.service import member_ids, notify
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

CATEGORY_GONE = "This category no longer exists."
CATEGORY_NAME_TAKEN = "A category with this name already exists."
NAME_TAKEN_ON_TARGET = "A category named “{name}” already exists on that board."
SAME_BOARD = "Choose another board."
CATEGORY_NOT_ARCHIVED = "Archive the category first."
CONFIRM_CATEGORY_NAME = "Type the category name to delete it permanently."
ITEM_GONE = "This item no longer exists."
UNDO_WINDOW = timedelta(seconds=60)  # how long after the delete "Undo" still works
# How long a deleted item's row is kept before the purge removes it for good.
SOFT_DELETE_PURGE_AFTER = timedelta(minutes=10)
_UNIQUE_VIOLATION = "23505"  # PostgreSQL SQLSTATEs
_FOREIGN_KEY_VIOLATION = "23503"
# How many numbered names suggest_category_name tries: "Dairy (2)" to "Dairy (100)".
_MAX_NAME_NUMBER = 100


@dataclass(frozen=True)
class ArchivedCategory:
    category: Category
    item_count: int
    purge_at: datetime | None  # when it is deleted for good; None: kept until deleted by hand


def _category_state(archived: bool | None) -> tuple[ColumnElement[bool], ...]:
    """The condition on ``Category`` for an active category (False), an archived one (True), or
    either (None)."""
    if archived is None:
        return ()
    return (Category.archived_at.is_not(None) if archived else Category.archived_at.is_(None),)


@cache
def _load_lookup(
    model: type[Category | Item],
    archived: bool | None,
    include_deleted: bool,
    action: Action,
    lock: RowLock | None,
) -> MemberLookup:
    """The statements of ``_load`` for one combination of its options, built once. The same
    visibility rules as ``boards.service._MEMBER_CATEGORY``: change them together."""
    query: Select = (
        select(model, BoardMember.role)
        .join(Board, Board.id == model.board_id)
        .join(BoardMember, BoardMember.board_id == model.board_id)
        .where(
            model.id == bindparam("object_id", type_=BigInteger),
            BoardMember.user_id == bindparam("user_id", type_=BigInteger),
            Board.archived_at.is_(None),
        )
    )
    if model is Item:
        query = query.join(Category, Category.id == Item.category_id)
        if not include_deleted:
            query = query.where(Item.deleted_at.is_(None))
    query = query.where(*_category_state(archived))
    return member_lookup(query, action, lock=lock, of=model)


def item_child_query(model: type[ChecklistEntry | Attachment], id_name: str) -> Select:
    """The query for one row of ``model``, a table with an ``item_id`` (a checklist entry, an
    attachment), with its item and ``BoardMember.role``, joined with the user's own membership.
    ``id_name`` and ``user_id`` are the bind parameters, ``load_for_member`` has their values.
    Like ``_load_lookup`` for an item, it leaves out a row whose item is deleted or in an archived
    category or board; change the rules together. The caller builds the lookup (``member_lookup``)
    once for each action and lock."""
    return (
        select(model, Item, BoardMember.role)
        .join(Item, Item.id == model.item_id)
        .join(Category, Category.id == Item.category_id)
        .join(Board, Board.id == Item.board_id)
        .join(BoardMember, BoardMember.board_id == Item.board_id)
        .where(
            model.id == bindparam(id_name, type_=BigInteger),
            BoardMember.user_id == bindparam("user_id", type_=BigInteger),
            Item.deleted_at.is_(None),
            Category.archived_at.is_(None),
            Board.archived_at.is_(None),
        )
    )


async def _load[M: (Category, Item)](
    db: AsyncSession,
    model: type[M],
    object_id: int,
    user: User,
    action: Action,
    gone: str,
    lock: RowLock,
    *,
    archived: bool | None = False,
    include_deleted: bool = False,
) -> tuple[M, BoardRole]:
    """The row and ``user``'s role on its board, in one query joined with ``board_members``, the
    board, which must not be archived, and the category (the row itself, or the item's):
    an active one, or with ``archived`` an archived one (True) or either (None). An item that
    was deleted (``deleted_at``) is not found either, unless ``include_deleted`` asks for it; that
    lifts only this condition, never the archived ones.

    For a change (any action but VIEW) the row stays locked in mode ``lock`` until the commit: a
    delete from another device that got there first makes this ``NotFound``, a later one waits
    for it. So does archiving, when the row is the category (an item's category is read, not
    locked). A member whose role does not allow the change takes no lock (see
    ``load_for_member``).
    """
    if not is_possible_id(object_id):
        raise NotFound(gone)
    row_lock = None if action is Action.VIEW else lock
    found, role = await load_for_member(
        db,
        _load_lookup(model, archived, include_deleted, action, row_lock),
        gone,
        {"object_id": object_id, "user_id": user.id},
    )
    return found, role


async def load_category(
    db: AsyncSession,
    category_id: int,
    user: User,
    action: Action,
    *,
    archived: bool | None = False,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
) -> tuple[Category, BoardRole]:
    """The category and ``user``'s role: an active category, or with ``archived`` an archived
    one (True) or either (None); for a change, locked in mode ``lock`` (see ``_load``)."""
    return await _load(
        db, Category, category_id, user, action, CATEGORY_GONE, lock, archived=archived
    )


async def load_item(
    db: AsyncSession,
    item_id: int,
    user: User,
    action: Action,
    *,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
    include_deleted: bool = False,
) -> tuple[Item, BoardRole]:
    """The item, in an active category, and ``user``'s role; a deleted one only with
    ``include_deleted``. For a change, locked in mode ``lock`` (see ``_load``)."""
    return await _load(
        db, Item, item_id, user, action, ITEM_GONE, lock, include_deleted=include_deleted
    )


def _category_changed(
    board_id: int, *category_ids: int, item_ids: Iterable[int] = ()
) -> BoardEvent:
    """The event for a change inside these categories. ``item_ids`` are the items whose details
    changed (an edit, a tick or a move, which set "Modified", or the checklist or the files), so
    that a page with one of them open fetches its details again; no duplicates in either list."""
    return BoardEvent(
        EventType.CATEGORY_CHANGED,
        board_id=board_id,
        category_ids=tuple(dict.fromkeys(category_ids)),
        item_ids=tuple(dict.fromkeys(item_ids)),
    )


def _clean_category_name(name: str) -> str:
    return clean_name(name, max_len=CATEGORY_NAME_MAX_LENGTH, label="Category name")


def _clean_item_name(name: str) -> str:
    return clean_name(name, max_len=ITEM_NAME_MAX_LENGTH, label="Item name")


@asynccontextmanager
async def _unique_category_name(db: AsyncSession) -> AsyncIterator[None]:
    """Write the new, renamed or restored category inside the block; a name an active category
    of the board has already (ignoring case) is a ``Conflict``.

    A savepoint keeps the failed write from rolling back, and so expiring, everything else the
    request loaded (such as the signed-in user, which the error page shows).
    """
    try:
        async with db.begin_nested():
            yield
    except IntegrityError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == _UNIQUE_VIOLATION:
            raise Conflict(CATEGORY_NAME_TAKEN) from None
        if sqlstate == _FOREIGN_KEY_VIOLATION:  # the board was deleted meanwhile
            raise NotFound(BOARD_GONE) from None
        raise


async def _next_position(db: AsyncSession, board_id: int) -> int:
    """The position after the board's last active category; 0 on a board without any."""
    last = await db.scalar(
        select(func.max(Category.position)).where(
            Category.board_id == board_id, Category.archived_at.is_(None)
        )
    )
    return 0 if last is None else last + 1


async def _top_position(db: AsyncSession, board_id: int) -> int:
    """The position before the board's first active category; 0 on a board without any. Not
    locked, like ``_next_position``: two new categories "on top" at once may share a position,
    and then the older one (by id) stays first."""
    first = await db.scalar(
        select(func.min(Category.position)).where(
            Category.board_id == board_id, Category.archived_at.is_(None)
        )
    )
    return 0 if first is None else first - 1


async def suggest_category_name(db: AsyncSession, board_id: int, name: str) -> str:
    """``name`` if no active category of the board has it (ignoring case), else the first free
    one of "name (2)" to "name (100)", ``name`` cut short to keep within 120 characters;
    ``Conflict`` when every one of them is taken."""
    candidates = [name]
    for number in range(2, _MAX_NAME_NUMBER + 1):
        suffix = f" ({number})"
        candidates.append(name[: CATEGORY_NAME_MAX_LENGTH - len(suffix)].rstrip() + suffix)
    for candidate in candidates:
        taken = await db.scalar(
            select(
                exists().where(
                    Category.board_id == board_id,
                    Category.archived_at.is_(None),
                    func.lower(Category.name) == func.lower(candidate),
                )
            )
        )
        if not taken:
            return candidate
    raise Conflict(CATEGORY_NAME_TAKEN)


async def create_category(
    db: AsyncSession, board_id: int, user: User, name: str, *, on_top: bool = False
) -> Category:
    """A new category at the end of the board, or ``on_top`` of it (before the first active one)."""
    board, _ = await require_board(db, board_id, user, Action.EDIT_CONTENT)
    name = _clean_category_name(name)
    position = await (_top_position if on_top else _next_position)(db, board.id)
    category = Category(board_id=board.id, name=name, position=position)
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


async def archive_category(db: AsyncSession, category_id: int, user: User) -> Category:
    """Archive the category: gone from the board, with its items, for every member, whose open
    pages drop it (``CATEGORY_DELETED``), until it is restored from the board's archive."""
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    # Not func.now(): the attribute would then expire, and async code cannot load it lazily.
    category.archived_at = datetime.now(UTC)
    await commit_and_publish(
        db,
        BoardEvent(
            EventType.CATEGORY_DELETED, board_id=category.board_id, category_ids=(category.id,)
        ),
    )
    return category


async def restore_category(db: AsyncSession, category_id: int, user: User) -> tuple[Category, bool]:
    """Bring an archived category back with its items, last on its board, and under a new name
    ("Dairy (2)") when an active category has taken its name meanwhile; returns it and whether
    it was renamed."""
    # Either state, so that a member who may not restore gets Forbidden for an active one too.
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT, archived=None)
    if category.archived_at is None:
        raise NotFound(CATEGORY_GONE)  # not in the archive (any more)
    # Both before the change: their queries would flush it first, outside the savepoint.
    name = await suggest_category_name(db, category.board_id, category.name)
    position = await _next_position(db, category.board_id)
    renamed = name != category.name
    async with _unique_category_name(db):
        category.archived_at = None
        category.name = name
        category.position = position
    await commit_and_publish(db, _category_changed(category.board_id, category.id))
    return category, renamed


async def move_category(
    db: AsyncSession, category_id: int, user: User, target_board_id: int, name: str
) -> tuple[Category, int]:
    """Move the category, with all its items, to the end of another board, under ``name``; returns
    it and the id of the board it left. ``user`` must be able to edit both boards.

    A name that an active category of the target board has already (ignoring case) is a
    ``NameTaken`` with the first free numbered name, which the dialog offers instead.

    The items need no update: their foreign key to the category (``category_id``, ``board_id``) is
    ``ON UPDATE CASCADE``, so the category's new board reaches every one of them, the deleted ones
    too (their "Undo" goes on working), without touching their ``updated_at``. Their checklist
    entries and files hang on the item, and so follow. People who are not members of the target
    board lose all of it at once. Everyone's own sort order for the category stays with it (the
    rows of ``category_sort_prefs``); those of people who cannot see it any more do nothing.

    The members of both boards, but ``user``, are told (``notify``), each once.
    """
    category, _ = await load_category(db, category_id, user, Action.EDIT_CONTENT)
    target, _ = await require_board(db, target_board_id, user, Action.EDIT_CONTENT)
    if target.id == category.board_id:
        raise ValidationFailed(SAME_BOARD)
    name = _clean_category_name(name)
    free = await suggest_category_name(db, target.id, name)
    if free != name:
        raise NameTaken(NAME_TAKEN_ON_TARGET.format(name=name), suggestion=free)
    # Before the change: their queries would flush it first, outside the savepoint. Who is told
    # needs the boards as they are now, and the board it leaves by name.
    position = await _next_position(db, target.id)
    source_id = category.board_id
    old_name = category.name
    source_name = await db.scalar(select(Board.name).where(Board.id == source_id))
    # Everyone who is on the target board is sent there (also those who are on both boards, once);
    # the rest of the source board's members, who only lose it, are sent to the board it left.
    # Not the one who moves it.
    on_target = await member_ids(db, target.id, exclude=user.id)
    on_source = set(await member_ids(db, source_id, exclude=user.id)) - set(on_target)
    async with _unique_category_name(db):  # also the target deleted meanwhile (a foreign key)
        category.board_id = target.id
        category.name = name
        category.position = position
        await db.flush()
    said = {"actor": user_display_name(user), "source": source_name, "target": target.name}
    # Each board's members read the category as they knew it: the source board's by the name it
    # had there (the move may have renamed it, e.g. to "Dairy (2)"), the target board's by its new one.
    told = [
        *notify(
            db,
            on_target,
            NotificationKind.CATEGORY_MOVED,
            board_id=target.id,
            category=name,
            **said,
        ),
        *notify(
            db,
            sorted(on_source),
            NotificationKind.CATEGORY_MOVED,
            board_id=source_id,
            category=old_name,
            **said,
        ),
    ]
    await commit_and_publish(
        db,
        BoardEvent(EventType.CATEGORY_DELETED, board_id=source_id, category_ids=(category.id,)),
        _category_changed(target.id, category.id),
        *told,
    )
    return category, source_id


async def delete_category(db: AsyncSession, category_id: int, user: User, confirm_name: str) -> int:
    """Delete an archived category for good, once ``confirm_name`` is its name (around spaces
    aside); its items go with it (FK cascade). Returns the id of its board."""
    category, _ = await load_category(
        db, category_id, user, Action.EDIT_CONTENT, archived=None, lock=RowLock.UPDATE
    )
    if category.archived_at is None:
        raise Conflict(CATEGORY_NOT_ARCHIVED)
    if confirm_name.strip() != category.name:
        raise ValidationFailed(CONFIRM_CATEGORY_NAME)
    await db.delete(category)
    await commit_and_publish(db)  # nobody can have an archived category open
    return category.board_id


async def list_archived_categories(
    db: AsyncSession, board_id: int, user: User
) -> tuple[Board, BoardRole, list[ArchivedCategory]]:
    """The board, ``user``'s role on it and its archived categories, the last archived first,
    each with how many items it holds and the moment it is deleted for good after the retention
    of the board's owner (whose setting decides, whoever archived it)."""
    board, role = await require_board(db, board_id, user, Action.VIEW)
    item_count = (
        select(func.count())
        .where(Item.category_id == Category.id, Item.deleted_at.is_(None))
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Category, item_count)
        .where(Category.board_id == board.id, Category.archived_at.is_not(None))
        .order_by(Category.archived_at.desc(), Category.id.desc())
    )
    days = await db.scalar(select(User.archive_retention_days).where(User.id == board.owner_id))
    # In Python, in UTC, as for archived boards (see boards.service.list_archived_boards).
    archived = [
        ArchivedCategory(
            category, count, None if days is None else category.archived_at + timedelta(days=days)
        )
        for category, count in rows
    ]
    return board, role, archived


async def reorder_categories(
    db: AsyncSession, board_id: int, user: User, ordered_ids: list[int]
) -> None:
    """Put the board's active categories in the order of ``ordered_ids``, which must name each of
    them exactly once (a client that missed a change gets ``ValidationFailed``)."""
    board, _ = await require_board(db, board_id, user, Action.EDIT_CONTENT)
    categories = await db.scalars(  # locked in id order: two reorders at once cannot deadlock
        select(Category)
        .where(Category.board_id == board.id, Category.archived_at.is_(None))
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
    """The category with its items in ``user``'s order for it, and ``user``'s role: two
    queries (the category with ``user``'s sort row, which an empty category needs too, and the
    items). Reading, so no lock."""
    if not is_possible_id(category_id):
        raise NotFound(CATEGORY_GONE)
    found = await select_member_category(db, user, category_id)
    if found is None:
        raise NotFound(CATEGORY_GONE)
    category, role, pref = found
    check_permission(role, Action.VIEW)
    items = await select_item_views(db, user, category_id=category.id)
    return CategoryView(category, items, sort_choice(user, pref)), role


@dataclass(frozen=True)
class AttachmentView:
    """An attached file and who uploaded it (``None`` when that account is gone)."""

    attachment: Attachment
    uploader: Author | None


@dataclass(frozen=True)
class ItemDetails:
    """What an item's details show to a member: the item, their role, who added it (``None``
    when that account is gone) and whether to name them (only on a board with more than one
    member), the checklist entries in their order and the attached files in theirs."""

    item: Item
    role: BoardRole
    author: Author | None
    show_author: bool
    entries: list[ChecklistEntry]
    attachments: list[AttachmentView]


_ITEM_ID = bindparam("item_id", type_=BigInteger)
# Built once, like the item views: the author's columns, whether the item's board is shared (not
# tied to a row: the members are counted once, not for every entry) and the entries, one row each.
_DETAILS_OF_ITEM = (
    select(
        User.id.label("author_id"),
        User.email.label("author_email"),
        User.display_name.label("author_name"),
        ChecklistEntry,
        (
            select(func.count())
            .where(BoardMember.board_id == bindparam("board_id", type_=BigInteger))
            .scalar_subquery()
            > 1
        ).label("shared"),
    )
    .select_from(Item)
    .outerjoin(User, User.id == Item.created_by)
    .outerjoin(ChecklistEntry, ChecklistEntry.item_id == Item.id)
    .where(Item.id == _ITEM_ID, Item.deleted_at.is_(None))
    .order_by(ChecklistEntry.position, ChecklistEntry.id)
)
# The files, one row each, with their uploader's columns (all NULL once that account is gone).
_FILES_OF_ITEM = (
    select(
        Attachment,
        User.id.label("uploader_id"),
        User.email.label("uploader_email"),
        User.display_name.label("uploader_name"),
    )
    .select_from(Attachment)
    .outerjoin(User, User.id == Attachment.uploaded_by)
    .where(Attachment.item_id == _ITEM_ID)
    .order_by(Attachment.created_at, Attachment.id)
)


def _author(user_id: int | None, email: str | None, name: str | None) -> Author | None:
    """The person with these columns, ``None`` for the NULLs of an account that is gone."""
    return None if user_id is None else Author(user_id, display_name(email, name))


async def get_item_details(db: AsyncSession, item_id: int, user: User) -> ItemDetails:
    """The item's details for any member (viewers too), in three statements: the item with
    ``user``'s role (``load_item``), then the author, whether the board is shared and the
    checklist in one, then the files. Reading, so no lock. An item that is gone, deleted or in an
    archived category or board is ``NotFound`` (the second statement leaves out an item that was
    deleted since the first, too)."""
    item, role = await load_item(db, item_id, user, Action.VIEW)
    rows = (
        await db.execute(_DETAILS_OF_ITEM, {"item_id": item.id, "board_id": item.board_id})
    ).all()
    if not rows:
        raise NotFound(ITEM_GONE)
    first = rows[0]  # the author and whether the board is shared are the same in every row
    author = _author(first.author_id, first.author_email, first.author_name)
    entries = [row.ChecklistEntry for row in rows if row.ChecklistEntry is not None]
    files = (await db.execute(_FILES_OF_ITEM, {"item_id": item.id})).all()
    attachments = [
        AttachmentView(
            row.Attachment, _author(row.uploader_id, row.uploader_email, row.uploader_name)
        )
        for row in files
    ]
    return ItemDetails(
        item, role, author, author is not None and first.shared, entries, attachments
    )


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
        created_by=user.id,
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
    await commit_and_publish(
        db, _category_changed(item.board_id, item.category_id, item_ids=(item.id,))
    )
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
    await commit_and_publish(
        db, _category_changed(item.board_id, item.category_id, item_ids=(item.id,))
    )
    return item


async def move_item(
    db: AsyncSession, item_id: int, user: User, target_category_id: int
) -> tuple[Item, int]:
    """Move the item to another active category of its own board; returns it and its old
    category id."""
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
            .where(
                Category.id == target_category_id,
                Category.board_id == item.board_id,
                Category.archived_at.is_(None),
            )
            .with_for_update(read=True)
        )
    # Also an archived one, or one on another board: an item changes boards only with its
    # category (move_category).
    if target is None:
        raise NotFound(CATEGORY_GONE)
    old_category_id = item.category_id
    item.category_id = target.id
    await commit_and_publish(
        db, _category_changed(item.board_id, old_category_id, target.id, item_ids=(item.id,))
    )
    return item, old_category_id


def _keep_updated_at(item: Item) -> None:
    """Delete and undo are no edit: have the UPDATE write ``updated_at`` back as it is (the
    column's ``onupdate`` stays silent for a value that is set). The attribute is expired after
    the flush, so do not read it afterwards (async code cannot load it lazily)."""
    item.updated_at = Item.updated_at


async def delete_item(db: AsyncSession, item_id: int, user: User) -> int:
    """Delete the item, for now: it is only marked, and ``restore_item`` brings it back for
    ``UNDO_WINDOW``. Returns the id of the category it was in."""
    # The default lock: marking the row changes no key, so a checklist entry or an attachment
    # being added to it at the same time does not wait.
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    # The clock that restore_item checks the window with, so not func.now(): that would expire
    # the attribute, and async code cannot load it lazily.
    item.deleted_at = datetime.now(UTC)
    _keep_updated_at(item)
    await commit_and_publish(db, _category_changed(item.board_id, item.category_id))
    return item.category_id


async def restore_item(db: AsyncSession, item_id: int, user: User) -> Item:
    """Bring the item back, just as it was, within ``UNDO_WINDOW`` of its deletion. An item that
    is not deleted, or whose window is over, or whose category or board was archived meanwhile,
    is gone (``NotFound``)."""
    # Role first: a viewer gets Forbidden whatever the clock says.
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT, include_deleted=True)
    if item.deleted_at is None or item.deleted_at <= datetime.now(UTC) - UNDO_WINDOW:
        raise NotFound(ITEM_GONE)
    item.deleted_at = None
    _keep_updated_at(item)
    await commit_and_publish(db, _category_changed(item.board_id, item.category_id))
    return item


async def clear_done(db: AsyncSession, category_id: int, user: User) -> int:
    """Delete the category's done items for good; returns how many there were. (Items deleted a
    moment ago are left alone: they wait for their "Undo".)"""
    category, _ = await load_category(
        db, category_id, user, Action.EDIT_CONTENT, lock=RowLock.SHARE
    )
    result = await db.execute(
        delete(Item).where(
            Item.category_id == category.id, Item.is_done.is_(True), Item.deleted_at.is_(None)
        )
    )
    removed = result.rowcount
    events = [_category_changed(category.board_id, category.id)] if removed else []
    await commit_and_publish(db, *events)
    return removed
