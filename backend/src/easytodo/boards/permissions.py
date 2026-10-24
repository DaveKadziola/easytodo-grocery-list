"""The permission check and the board lookup every board-scoped request goes through, and the row
locks a change takes. What each role may do (``Action``, ``can``, ``roles_for``) is in ``roles``,
and is importable from here too."""

from enum import StrEnum, auto
from functools import cache
from typing import Any, NamedTuple

from sqlalchemy import BigInteger, ColumnElement, Row, Select, bindparam, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.roles import Action, can, roles_for
from easytodo.errors import Forbidden, NotFound
from easytodo.models import Board, BoardMember, BoardRole, User

_MAX_ID = 2**63 - 1  # ids are BigIntegers; a larger one cannot exist (and would fail in SQL)
BOARD_GONE = "Board not found."


class RowLock(StrEnum):
    """The lock a change takes on the row it loads, held until the commit: the weakest one that
    keeps the change right, so changes that do not get in each other's way do not wait."""

    SHARE = auto()  # FOR SHARE: the row stays as it is (the parent of rows added or deleted)
    NO_KEY_UPDATE = auto()  # FOR NO KEY UPDATE: the change updates the row, not its keys
    UPDATE = auto()  # FOR UPDATE: the change deletes the row


_FOR_UPDATE_ARGS: dict[RowLock, dict[str, bool]] = {
    RowLock.SHARE: {"read": True},
    RowLock.NO_KEY_UPDATE: {"key_share": True},
    RowLock.UPDATE: {},
}


def check_permission(role: BoardRole, action: Action) -> None:
    if not can(role, action):
        raise Forbidden("You don't have permission to do that.")


def is_possible_id(value: int) -> bool:
    """Whether ``value`` can be a row id at all; look up only those that can."""
    return 0 < value <= _MAX_ID


def _board_state(archived: bool | None) -> tuple[ColumnElement[bool], ...]:
    """The condition on ``Board`` for an active board (False), an archived one (True), or
    either (None)."""
    if archived is None:
        return ()
    return (Board.archived_at.is_not(None) if archived else Board.archived_at.is_(None),)


class MemberLookup(NamedTuple):
    """The statements of a ``load_for_member`` call, built by ``member_lookup``."""

    query: Select[Any]
    locked: Select[Any] | None  # ``query`` for a change: with the role filter and the row lock
    action: Action


def member_lookup(
    query: Select[Any], action: Action, *, lock: RowLock | None = None, of: Any
) -> MemberLookup:
    """``query`` and, with ``lock``, its locked variant, for ``load_for_member``. ``query`` selects
    what was asked for and ``BoardMember.role``, joined with the user's own membership, and takes
    its ids as bind parameters (``load_for_member`` has their values).

    ``of`` is the table (model) whose row ``lock`` is for, and must always be given: a lock
    without it would take a row of every table in the join, the memberships and the board too.

    Building a statement and working out its cache key takes longer than running it, so the
    caller builds the lookup once for each combination of what shapes it (the model, ``archived``,
    ``action``, ``lock``...) and keeps it (``functools.cache``); never one lookup per request, and
    never a cache keyed by the ``query`` itself, which a caller may build anew each time.
    """
    locked = None
    if lock is not None:
        locked = query.where(BoardMember.role.in_(roles_for(action))).with_for_update(
            of=of, **_FOR_UPDATE_ARGS[lock]
        )
    return MemberLookup(query, locked, action)


async def load_for_member(
    db: AsyncSession, lookup: MemberLookup, gone: str, params: dict[str, Any]
) -> Row[Any]:
    """The one row of ``lookup`` for the bind parameters ``params``. ``NotFound(gone)`` when there
    is none, ``Forbidden`` when the role does not allow ``lookup.action``.

    With a lock, that row stays locked in that mode until the commit, and the query takes only a
    role that allows the action: a member who may not do it neither takes the lock nor waits for
    anyone else's. Only when that finds nothing does the query run again without lock and role,
    to tell a missing row (404) from a missing permission (403); if the role allows the action by
    then, something changed in between (a role, a move), which is a 404 too, so the page
    reloads.
    """
    if lookup.locked is not None:
        row = (await db.execute(lookup.locked, params)).one_or_none()
        if row is not None:
            return row
    row = (await db.execute(lookup.query, params)).one_or_none()
    if row is None:
        raise NotFound(gone)
    check_permission(row.role, lookup.action)
    if lookup.locked is not None:
        raise NotFound(gone)
    return row


@cache
def _board_lookup(archived: bool | None, action: Action, lock: RowLock | None) -> MemberLookup:
    query = (
        select(Board, BoardMember.role)
        .join(BoardMember, BoardMember.board_id == Board.id)
        .where(
            Board.id == bindparam("board_id", type_=BigInteger),
            BoardMember.user_id == bindparam("user_id", type_=BigInteger),
            *_board_state(archived),
        )
    )
    return member_lookup(query, action, lock=lock, of=Board)


async def require_board(
    db: AsyncSession,
    board_id: int,
    user: User,
    action: Action,
    *,
    archived: bool | None = False,
    lock: RowLock | None = None,
) -> tuple[Board, BoardRole]:
    """The board and ``user``'s role in it, read from ``board_members``: an active board, or
    with ``archived`` an archived one (True) or either (None). With ``lock``, the board's row
    stays locked in that mode until the commit.

    A board the user is not a member of answers like one that does not exist (``NotFound``),
    for admins too, and so does an archived board unless ``archived`` asks for it; a member whose
    role lacks ``action`` gets ``Forbidden``.
    """
    if not is_possible_id(board_id):
        raise NotFound(BOARD_GONE)
    board, role = await load_for_member(
        db,
        _board_lookup(archived, action, lock),
        BOARD_GONE,
        {"board_id": board_id, "user_id": user.id},
    )
    return board, role
