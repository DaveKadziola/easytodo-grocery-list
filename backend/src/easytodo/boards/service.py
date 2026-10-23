"""Creating, listing, renaming, archiving, restoring and deleting boards, loading a whole board
for display, the one query that lists items (``select_item_views``), and the board's members:
listing them, changing their roles, removing them and leaving.

An archived board is gone for its members (``require_board`` and every list leave it out) but
keeps its data, until its owner restores it or deletes it for good from the archive.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    Row,
    ScalarSelect,
    Select,
    String,
    and_,
    bindparam,
    case,
    false,
    func,
    not_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from easytodo.boards.permissions import Action, RowLock, is_possible_id, require_board
from easytodo.content.validation import clean_name
from easytodo.display import display_name
from easytodo.errors import Conflict, Forbidden, NotFound, ValidationFailed
from easytodo.models import (
    SORT_FIELDS,
    Attachment,
    Board,
    BoardMember,
    BoardRole,
    Category,
    CategorySortPref,
    ChecklistEntry,
    Item,
    User,
)
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

_NAME_MAX_LENGTH = 80  # boards.name is String(80)
OWNER_ROLE_FIXED = "The owner's role can't be changed."
OWNER_CANNOT_LEAVE = "The owner can't leave. Archive the board instead."
BOARD_NOT_ARCHIVED = "Archive the board first."
CONFIRM_BOARD_NAME = "Type the board name to delete it permanently."


@dataclass(frozen=True)
class BoardSummary:
    board: Board
    role: BoardRole
    member_count: int
    open_items: int
    owner_email: str


@dataclass(frozen=True)
class ArchivedBoard:
    board: Board
    purge_at: datetime | None  # when it is deleted for good; None: kept until its owner does it


@dataclass(frozen=True)
class Author:
    """Who added an item: the name they are shown by (their own, or the email's local part)."""

    user_id: int
    name: str


@dataclass(frozen=True)
class ItemView:
    """An item as the board shows it: with its author (``None`` once their account is gone),
    whether to show the author (only on a board with more than one member), and the counts of
    its checklist entries and attachments."""

    item: Item
    author: Author | None
    show_author: bool
    checklist_done: int
    checklist_total: int
    attachment_count: int


@dataclass(frozen=True)
class SortChoice:
    """How one user sees one category: ``custom`` when they chose it for that category, else
    their account's default."""

    field: str  # one of SORT_FIELDS
    descending: bool
    open_first: bool
    custom: bool


@dataclass(frozen=True)
class CategoryView:
    category: Category
    items: list[ItemView]
    sort: SortChoice


@dataclass(frozen=True)
class BoardView:
    board: Board
    role: BoardRole
    categories: list[CategoryView]


@dataclass(frozen=True)
class MemberView:
    user_id: int
    email: str
    role: BoardRole
    joined_at: datetime


def _clean_name(name: str) -> str:
    """As category and item names: stripped, inner whitespace runs as one space, no NUL
    (PostgreSQL text cannot hold it)."""
    return clean_name(name, max_len=_NAME_MAX_LENGTH, label="Board name")


async def add_board(db: AsyncSession, owner: User, name: str) -> Board:
    """A new board with ``owner`` as its owner member, in ``db``'s open transaction (not
    committed: for callers that fill the board before they commit)."""
    board = Board(name=_clean_name(name), owner_id=owner.id)
    db.add(board)
    await db.flush()
    db.add(BoardMember(board_id=board.id, user_id=owner.id, role=BoardRole.OWNER))
    return board


async def create_board(db: AsyncSession, owner: User, name: str) -> Board:
    """A new board with ``owner`` as its owner member (committed with anything pending)."""
    board = await add_board(db, owner, name)
    await commit_and_publish(db)  # nobody can be watching a board that did not exist
    return board


async def is_member(db: AsyncSession, board_id: int, user: User) -> bool:
    return (
        await db.scalar(
            select(BoardMember.user_id).where(
                BoardMember.board_id == board_id, BoardMember.user_id == user.id
            )
        )
        is not None
    )


async def landing_board_id(db: AsyncSession, user: User, last_opened: int | None) -> int | None:
    """The board to open the app on: ``last_opened`` (the last board this device showed) while
    ``user`` is still a member of it, else their only board; None when there is no such one.
    Archived boards do not count."""
    board_ids = list(
        await db.scalars(
            select(BoardMember.board_id)
            .join(Board, Board.id == BoardMember.board_id)
            .where(BoardMember.user_id == user.id, Board.archived_at.is_(None))
        )
    )
    if last_opened in board_ids:
        return last_opened
    return board_ids[0] if len(board_ids) == 1 else None


async def list_boards(db: AsyncSession, user: User) -> list[BoardSummary]:
    """The active boards ``user`` is a member of: their own first, then the shared ones, each by
    name; one query, with each board's owner's e-mail and its open items (in active categories,
    not deleted)."""
    owner = aliased(User)
    members = aliased(BoardMember)
    member_count = select(func.count()).where(members.board_id == Board.id).scalar_subquery()
    open_items = (
        select(func.count())
        .select_from(Item)
        .join(Category, Category.id == Item.category_id)
        .where(
            Item.board_id == Board.id,
            Item.is_done.is_(False),
            Item.deleted_at.is_(None),
            Category.archived_at.is_(None),
        )
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Board, BoardMember.role, member_count, open_items, owner.email)
        .join(BoardMember, BoardMember.board_id == Board.id)
        .join(owner, owner.id == Board.owner_id)
        .where(BoardMember.user_id == user.id, Board.archived_at.is_(None))
        .order_by((Board.owner_id == user.id).desc(), func.lower(Board.name), Board.id)
    )
    return [BoardSummary(*row) for row in rows]


def _archived_by(user: User) -> tuple[ColumnElement[bool], ...]:
    return Board.owner_id == user.id, Board.archived_at.is_not(None)


async def list_archived_boards(db: AsyncSession, user: User) -> list[ArchivedBoard]:
    """``user``'s own archived boards, the last archived first, each with the moment it is
    deleted for good after ``user``'s retention (the owner's setting decides)."""
    boards = await db.scalars(
        select(Board).where(*_archived_by(user)).order_by(Board.archived_at.desc(), Board.id.desc())
    )
    days = user.archive_retention_days
    # In Python, in UTC: an interval added in SQL would follow the session's time zone, and be
    # an hour off across a daylight saving change from the moment the purge deletes the board.
    return [
        ArchivedBoard(board, None if days is None else board.archived_at + timedelta(days=days))
        for board in boards
    ]


async def count_archived_boards(db: AsyncSession, user: User) -> int:
    """How many archived boards ``user`` owns."""
    return await db.scalar(select(func.count()).select_from(Board).where(*_archived_by(user)))


async def rename_board(db: AsyncSession, board_id: int, user: User, name: str) -> Board:
    board, _ = await require_board(
        db, board_id, user, Action.MANAGE_BOARD, lock=RowLock.NO_KEY_UPDATE
    )
    board.name = _clean_name(name)
    await commit_and_publish(db, BoardEvent(EventType.BOARD_UPDATED, board_id=board.id))
    return board


async def archive_board(db: AsyncSession, board_id: int, user: User) -> Board:
    """Archive the board: gone for its members, whose open pages leave it (``BOARD_DELETED``),
    until its owner restores it. Its members, invites, categories and items are kept."""
    board, _ = await require_board(
        db, board_id, user, Action.MANAGE_BOARD, lock=RowLock.NO_KEY_UPDATE
    )
    # Not func.now(): the attribute would then expire, and async code cannot load it lazily.
    board.archived_at = datetime.now(UTC)
    await commit_and_publish(db, BoardEvent(EventType.BOARD_DELETED, board_id=board.id))
    return board


async def restore_board(db: AsyncSession, board_id: int, user: User) -> Board:
    """Bring an archived board back, with everything on it, for all its members."""
    board, _ = await require_board(
        db, board_id, user, Action.MANAGE_BOARD, archived=True, lock=RowLock.NO_KEY_UPDATE
    )
    board.archived_at = None
    await commit_and_publish(db)  # nobody can have an archived board open
    return board


async def delete_board(db: AsyncSession, board_id: int, user: User, confirm_name: str) -> None:
    """Delete an archived board for good, once ``confirm_name`` is its name (around spaces
    aside); its members, invites, categories and items go with it (FK cascades)."""
    board, _ = await require_board(
        db, board_id, user, Action.MANAGE_BOARD, archived=None, lock=RowLock.UPDATE
    )
    if board.archived_at is None:
        raise Conflict(BOARD_NOT_ARCHIVED)
    if confirm_name.strip() != board.name:
        raise ValidationFailed(CONFIRM_BOARD_NAME)
    await db.delete(board)
    await commit_and_publish(db)  # nobody can have an archived board open


def sort_choice(user: User, pref: CategorySortPref | None) -> SortChoice:
    """``user``'s order for a category: ``pref``, their own choice for it, wins over the default
    of their account."""
    if pref is not None:
        return SortChoice(pref.field, pref.descending, pref.open_first, custom=True)
    return SortChoice(user.sort_field, user.sort_descending, user.sort_open_first, custom=False)


# The user's own row for a category, when they have one (select_item_views joins it).
_SORT_PREF = aliased(CategorySortPref, name="pref")
# What each sort field orders by, ascending.
_SORT_COLUMNS: dict[str, ColumnElement] = {
    "name": func.lower(Item.name),
    "created": Item.created_at,
    "updated": Item.updated_at,
}


# The account's default order, as the parameters the item statements take (the user's values go
# in per call, so that the statements and their SQL are the same for everyone).
_DEFAULT_FIELD = bindparam("sort_field", type_=String)
_DEFAULT_DESCENDING = bindparam("sort_descending", type_=Boolean)
_DEFAULT_OPEN_FIRST = bindparam("sort_open_first", type_=Boolean)


def _item_order() -> list[ColumnElement]:
    """How items are listed in a category, on the board page and in every fragment alike: in the
    user's order for the category (their row in ``_SORT_PREF``, which the statement joins), else
    in their account's default.

    The field and the direction pick one of the CASE terms; the others are NULL for every row,
    so they order nothing. Ties go by id.
    """
    field = func.coalesce(_SORT_PREF.field, _DEFAULT_FIELD)
    descending = func.coalesce(_SORT_PREF.descending, _DEFAULT_DESCENDING)
    open_first = func.coalesce(_SORT_PREF.open_first, _DEFAULT_OPEN_FIRST)
    order: list[ColumnElement] = [case((open_first, Item.is_done), else_=false())]
    for name in SORT_FIELDS:  # a field without a column in _SORT_COLUMNS fails here, at import
        column = _SORT_COLUMNS[name]
        order.append(case((and_(field == name, not_(descending)), column)))
        order.append(case((and_(field == name, descending), column)).desc())
    order.append(Item.id)
    return order


def _account_sort(user: User) -> dict[str, Any]:
    """The parameters of the item statements that carry ``user``'s account default."""
    return {
        "sort_field": user.sort_field,
        "sort_descending": user.sort_descending,
        "sort_open_first": user.sort_open_first,
    }


_AUTHOR = aliased(User)
_BOARD_ID = bindparam("board_id", type_=BigInteger)
_CATEGORY_ID = bindparam("category_id", type_=BigInteger)
_USER_ID = bindparam("user_id", type_=BigInteger)


def _item_views_query(board_ref: ColumnElement | ScalarSelect) -> Select[Any]:
    """The items with their author's columns, whether the board is shared (the board ``board_ref``
    has more than one member) and the counts of checklist entries (done, all) and attachments;
    joined with the viewer's own sort row of each item's category (the ``user_id`` parameter), for
    the order; in no scope yet, and in no order.

    The counts are LATERAL subqueries, one for the checklist (done and all in one scan of the
    item's entries) and one for the attachments, so an item costs two index lookups, not three,
    and the plan cannot degrade into one that sums whole tables for every item (what a grouped
    join does when the planner's row estimates are stale). An aggregate without GROUP BY always
    returns one row, so joining them ``ON true`` loses no item.
    """
    # Not tied to an item's row: PostgreSQL counts the members once, not for every item.
    shared = select(func.count()).where(BoardMember.board_id == board_ref).scalar_subquery() > 1
    entries = (
        select(
            func.count().label("total"),
            func.count().filter(ChecklistEntry.is_done.is_(True)).label("done"),
        )
        .where(ChecklistEntry.item_id == Item.id)
        .lateral("entries")
    )
    files = (
        select(func.count().label("total")).where(Attachment.item_id == Item.id).lateral("files")
    )
    return (
        select(
            Item,
            _AUTHOR.id,
            _AUTHOR.email,
            _AUTHOR.display_name,
            shared,
            entries.c.done,
            entries.c.total,
            files.c.total,
        )
        .join(Category, Category.id == Item.category_id)
        .outerjoin(_AUTHOR, _AUTHOR.id == Item.created_by)
        # The user in the ON clause, not in WHERE: an item of a category without a row stays.
        .outerjoin(
            _SORT_PREF,
            and_(_SORT_PREF.category_id == Item.category_id, _SORT_PREF.user_id == _USER_ID),
        )
        .join(entries, true())
        .join(files, true())
        .where(Category.archived_at.is_(None), Item.deleted_at.is_(None))
    )


# Built once, here, with their order: building such a statement and working out its cache key
# takes longer than running it (the order alone, 0.4 ms), and every board and category view runs
# one. Per call only the values of the parameters change.
_BOARD_ITEMS = (
    _item_views_query(_BOARD_ID).where(Item.board_id == _BOARD_ID).order_by(*_item_order())
)
# An alias of its own: the subquery would correlate to the joined ``Category`` and have no table.
_in_category = aliased(Category)
_CATEGORY_ITEMS = (
    _item_views_query(
        select(_in_category.board_id).where(_in_category.id == _CATEGORY_ID).scalar_subquery()
    )
    .where(Item.category_id == _CATEGORY_ID)
    .order_by(*_item_order())
)


async def select_item_views(
    db: AsyncSession, user: User, *, board_id: int | None = None, category_id: int | None = None
) -> list[ItemView]:
    """The items of a board (all its active categories) or of one category, in ``user``'s order
    for each category (see ``_item_order``): not deleted, and not in an archived category. One
    statement, and no permission check: the caller has found the board or category for ``user``
    first (``require_board``, ``load_category``). Exactly one of ``board_id`` and ``category_id``
    is given."""
    if (board_id is None) == (category_id is None):
        raise TypeError("Give exactly one of board_id and category_id.")
    if board_id is not None:
        query, params = _BOARD_ITEMS, {"board_id": board_id}
    else:
        query, params = _CATEGORY_ITEMS, {"category_id": category_id}
    rows = await db.execute(query, params | {"user_id": user.id} | _account_sort(user))

    authors: dict[int, Author] = {}  # one Author per person
    views = []
    for item, author_id, email, name, is_shared, done, total, files in rows:
        person = None
        if author_id is not None:
            person = authors.get(author_id)
            if person is None:
                person = authors[author_id] = Author(author_id, display_name(email, name))
        views.append(ItemView(item, person, person is not None and is_shared, done, total, files))
    return views


_OWN_SORT = (
    select(CategorySortPref)
    .where(CategorySortPref.user_id == _USER_ID, CategorySortPref.category_id == _CATEGORY_ID)
    .execution_options(populate_existing=True)
)


async def select_sort_pref(
    db: AsyncSession, user: User, category_id: int
) -> CategorySortPref | None:
    """``user``'s own sort row for the category, if they have made one; no permission check, as
    for ``select_item_views``. Read afresh (populate_existing): the rows are written with Core
    statements, which leave the identity map as it was."""
    return await db.scalar(_OWN_SORT, {"user_id": user.id, "category_id": category_id})


# An active category of an active board with the role of the member asking and their own sort row
# of it, when they have one: one statement, as an empty category has no item row to carry the sort
# row. Built once, with populate_existing like the statements around it.
_MEMBER_CATEGORY = (
    select(Category, BoardMember.role, _SORT_PREF)
    .join(Board, Board.id == Category.board_id)
    .join(BoardMember, BoardMember.board_id == Category.board_id)
    .outerjoin(
        _SORT_PREF, and_(_SORT_PREF.category_id == Category.id, _SORT_PREF.user_id == _USER_ID)
    )
    .where(
        Category.id == _CATEGORY_ID,
        BoardMember.user_id == _USER_ID,
        Board.archived_at.is_(None),
        Category.archived_at.is_(None),
    )
    .execution_options(populate_existing=True)
)


async def select_member_category(
    db: AsyncSession, user: User, category_id: int
) -> Row[tuple[Category, BoardRole, CategorySortPref | None]] | None:
    """The row ``(category, role, sort row)``: the active category (of an active board), ``user``'s
    role on its board and ``user``'s own sort row of it, if they have made one; ``None`` when there
    is no such category or ``user`` is not a member of its board.

    One statement and no lock: for reading (a change goes through ``content.service.load_category``).
    ``category_id`` must be a possible id (``is_possible_id``).
    """
    result = await db.execute(_MEMBER_CATEGORY, {"category_id": category_id, "user_id": user.id})
    return result.one_or_none()


# The board's active categories with the viewer's own sort row of each, built once like the item
# statements above. populate_existing: a row the session loaded earlier may be out of date (the
# rows are written with Core statements, which leave the identity map as it was).
_BOARD_CATEGORIES = (
    select(Category, _SORT_PREF)
    .outerjoin(
        _SORT_PREF, and_(_SORT_PREF.category_id == Category.id, _SORT_PREF.user_id == _USER_ID)
    )
    .where(Category.board_id == _BOARD_ID, Category.archived_at.is_(None))
    .order_by(Category.position, Category.id)
    .execution_options(populate_existing=True)
)


async def get_board_view(db: AsyncSession, board_id: int, user: User) -> BoardView:
    """The board with its active categories (by position) and their items, each category in
    ``user``'s order for it (``SortChoice``).

    Two queries after the permission check: the categories with ``user``'s sort rows, and the
    items in them (``select_item_views``).
    """
    board, role = await require_board(db, board_id, user, Action.VIEW)
    rows = await db.execute(_BOARD_CATEGORIES, {"board_id": board.id, "user_id": user.id})
    categories = rows.all()
    by_category: dict[int, list[ItemView]] = {}
    for item_view in await select_item_views(db, user, board_id=board.id):
        by_category.setdefault(item_view.item.category_id, []).append(item_view)
    views = [
        CategoryView(c, by_category.get(c.id, []), sort_choice(user, pref))
        for c, pref in categories
    ]
    return BoardView(board, role, views)


async def list_members(db: AsyncSession, board_id: int, user: User) -> list[MemberView]:
    """The board's members: the owner first, then by e-mail."""
    board, _ = await require_board(db, board_id, user, Action.VIEW)
    rows = await db.execute(
        select(User.id, User.email, BoardMember.role, BoardMember.joined_at)
        .join(BoardMember, BoardMember.user_id == User.id)
        .where(BoardMember.board_id == board.id)
        .order_by((BoardMember.role == BoardRole.OWNER).desc(), User.email)
    )
    return [MemberView(*row) for row in rows]


async def _member(db: AsyncSession, board_id: int, member_user_id: int) -> BoardMember:
    member = None
    if is_possible_id(member_user_id):
        member = await db.get(BoardMember, (board_id, member_user_id))
    if member is None:
        raise NotFound("Member not found.")
    return member


async def change_role(
    db: AsyncSession, board_id: int, user: User, member_user_id: int, role: BoardRole
) -> None:
    """Make the member an editor or a viewer; the owner stays the owner, and nobody becomes one."""
    await require_board(db, board_id, user, Action.MANAGE_MEMBERS)
    member = await _member(db, board_id, member_user_id)
    if role == BoardRole.OWNER or member.role == BoardRole.OWNER:
        raise Forbidden(OWNER_ROLE_FIXED)
    member.role = role
    # Names the member: their own open board pages need a full reload, as their controls change.
    await commit_and_publish(
        db, BoardEvent(EventType.BOARD_UPDATED, board_id=board_id, user_id=member_user_id)
    )


async def remove_member(db: AsyncSession, board_id: int, user: User, member_user_id: int) -> None:
    await require_board(db, board_id, user, Action.MANAGE_MEMBERS)
    member = await _member(db, board_id, member_user_id)
    if member.role == BoardRole.OWNER:  # only owners manage members, so: the owner leaving
        raise Conflict(OWNER_CANNOT_LEAVE)
    await _delete_member(db, member)


async def leave_board(db: AsyncSession, board_id: int, user: User) -> None:
    _, role = await require_board(db, board_id, user, Action.VIEW)
    if role == BoardRole.OWNER:
        raise Conflict(OWNER_CANNOT_LEAVE)
    await _delete_member(db, await _member(db, board_id, user.id))


async def _delete_member(db: AsyncSession, member: BoardMember) -> None:
    """Delete the membership; ``MEMBER_REMOVED`` names the user, whose open pages are closed."""
    await db.delete(member)
    await commit_and_publish(
        db,
        BoardEvent(EventType.MEMBER_REMOVED, board_id=member.board_id, user_id=member.user_id),
    )
