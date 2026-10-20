"""Creating, listing, renaming and deleting boards, and loading a whole board for display."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from easytodo.boards.permissions import Action, require_board
from easytodo.errors import ValidationFailed
from easytodo.models import Board, BoardMember, BoardRole, Category, Item, User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

_NAME_MAX_LENGTH = 80  # boards.name is String(80)

# How items are listed in a category, on the board page and in every fragment alike: open ones
# first, then by name ignoring case.
ITEM_ORDER = (Item.is_done, func.lower(Item.name), Item.id)


@dataclass(frozen=True)
class BoardSummary:
    board: Board
    role: BoardRole
    member_count: int
    open_items: int


@dataclass(frozen=True)
class CategoryView:
    category: Category
    items: list[Item]


@dataclass(frozen=True)
class BoardView:
    board: Board
    role: BoardRole
    categories: list[CategoryView]


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= _NAME_MAX_LENGTH:
        raise ValidationFailed("Board name must be 1–80 characters.")
    return name


async def create_board(db: AsyncSession, owner: User, name: str) -> Board:
    """A new board with ``owner`` as its owner member (committed with anything pending)."""
    board = Board(name=_clean_name(name), owner_id=owner.id)
    db.add(board)
    await db.flush()
    db.add(BoardMember(board_id=board.id, user_id=owner.id, role=BoardRole.OWNER))
    await commit_and_publish(db)  # nobody can be watching a board that did not exist
    return board


async def list_boards(db: AsyncSession, user: User) -> list[BoardSummary]:
    """The boards ``user`` is a member of: their own first, then the shared ones, each by name."""
    members = aliased(BoardMember)
    member_count = select(func.count()).where(members.board_id == Board.id).scalar_subquery()
    open_items = (
        select(func.count())
        .select_from(Item)
        .where(Item.board_id == Board.id, Item.is_done.is_(False))
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Board, BoardMember.role, member_count, open_items)
        .join(BoardMember, BoardMember.board_id == Board.id)
        .where(BoardMember.user_id == user.id)
        .order_by((Board.owner_id == user.id).desc(), func.lower(Board.name), Board.id)
    )
    return [BoardSummary(*row) for row in rows]


async def rename_board(db: AsyncSession, board_id: int, user: User, name: str) -> Board:
    board, _ = await require_board(db, board_id, user, Action.MANAGE_BOARD)
    board.name = _clean_name(name)
    await commit_and_publish(db, BoardEvent(EventType.BOARD_UPDATED, board_id=board.id))
    return board


async def delete_board(db: AsyncSession, board_id: int, user: User) -> None:
    """Delete the board; its members, invites, categories and items go with it (FK cascades)."""
    board, _ = await require_board(db, board_id, user, Action.MANAGE_BOARD)
    await db.delete(board)
    await commit_and_publish(db, BoardEvent(EventType.BOARD_DELETED, board_id=board.id))


async def get_board_view(db: AsyncSession, board_id: int, user: User) -> BoardView:
    """The board with its categories (by position) and their items (open first, then by name).

    Two queries after the permission check: the categories and all of the board's items.
    """
    board, role = await require_board(db, board_id, user, Action.VIEW)
    categories = list(
        await db.scalars(
            select(Category)
            .where(Category.board_id == board.id)
            .order_by(Category.position, Category.id)
        )
    )
    items = await db.scalars(select(Item).where(Item.board_id == board.id).order_by(*ITEM_ORDER))
    by_category: dict[int, list[Item]] = {}
    for item in items:
        by_category.setdefault(item.category_id, []).append(item)
    views = [CategoryView(c, by_category.get(c.id, [])) for c in categories]
    return BoardView(board, role, views)
