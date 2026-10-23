"""Creating, listing, renaming, archiving, restoring and deleting boards, loading a whole board
for display, and the board's members: listing them, changing their roles, removing them and
leaving.

An archived board is gone for its members (``require_board`` and every list leave it out) but
keeps its data, until its owner restores it or deletes it for good from the archive.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from easytodo.boards.permissions import Action, RowLock, is_possible_id, require_board
from easytodo.content.validation import clean_name
from easytodo.errors import Conflict, Forbidden, NotFound, ValidationFailed
from easytodo.models import Board, BoardMember, BoardRole, Category, Item, User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

_NAME_MAX_LENGTH = 80  # boards.name is String(80)
OWNER_ROLE_FIXED = "The owner's role can't be changed."
OWNER_CANNOT_LEAVE = "The owner can't leave. Archive the board instead."
BOARD_NOT_ARCHIVED = "Archive the board first."
CONFIRM_BOARD_NAME = "Type the board name to delete it permanently."

# How items are listed in a category, on the board page and in every fragment alike: open ones
# first, then by name ignoring case.
ITEM_ORDER = (Item.is_done, func.lower(Item.name), Item.id)


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
class CategoryView:
    category: Category
    items: list[Item]


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


async def get_board_view(db: AsyncSession, board_id: int, user: User) -> BoardView:
    """The board with its active categories (by position) and their items (open first, then by
    name).

    Two queries after the permission check: the categories and the items in them.
    """
    board, role = await require_board(db, board_id, user, Action.VIEW)
    categories = list(
        await db.scalars(
            select(Category)
            .where(Category.board_id == board.id, Category.archived_at.is_(None))
            .order_by(Category.position, Category.id)
        )
    )
    items = await db.scalars(
        select(Item)
        .join(Category, Category.id == Item.category_id)
        .where(Item.board_id == board.id, Item.deleted_at.is_(None), Category.archived_at.is_(None))
        .order_by(*ITEM_ORDER)
    )
    by_category: dict[int, list[Item]] = {}
    for item in items:
        by_category.setdefault(item.category_id, []).append(item)
    views = [CategoryView(c, by_category.get(c.id, [])) for c in categories]
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
