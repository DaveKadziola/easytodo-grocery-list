"""What each board role may do, and the board lookup every board-scoped request goes through."""

from enum import StrEnum, auto

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.errors import Forbidden, NotFound
from easytodo.models import Board, BoardMember, BoardRole, User


class Action(StrEnum):
    VIEW = auto()
    EDIT_CONTENT = auto()
    MANAGE_MEMBERS = auto()
    MANAGE_BOARD = auto()


_ALLOWED: dict[BoardRole, frozenset[Action]] = {
    BoardRole.VIEWER: frozenset({Action.VIEW}),
    BoardRole.EDITOR: frozenset({Action.VIEW, Action.EDIT_CONTENT}),
    BoardRole.OWNER: frozenset(Action),
}

_MAX_ID = 2**63 - 1  # ids are BigIntegers; a larger one cannot exist (and would fail in SQL)


def can(role: BoardRole, action: Action) -> bool:
    return action in _ALLOWED[role]


async def require_board(
    db: AsyncSession, board_id: int, user: User, action: Action
) -> tuple[Board, BoardRole]:
    """The board and ``user``'s role in it, read from ``board_members``.

    A board the user is not a member of answers like one that does not exist (``NotFound``),
    for admins too; a member whose role lacks ``action`` gets ``Forbidden``.
    """
    row = None
    if 0 < board_id <= _MAX_ID:
        row = (
            await db.execute(
                select(Board, BoardMember.role)
                .join(BoardMember, BoardMember.board_id == Board.id)
                .where(Board.id == board_id, BoardMember.user_id == user.id)
            )
        ).one_or_none()
    if row is None:
        raise NotFound("Board not found.")
    board, role = row
    if not can(role, action):
        raise Forbidden("You don't have permission to do that.")
    return board, role
