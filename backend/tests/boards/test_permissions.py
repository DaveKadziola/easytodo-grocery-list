from datetime import UTC, datetime

import pytest
from factories import create_board, create_user

from easytodo.boards.permissions import Action, RowLock, can, require_board, roles_for
from easytodo.errors import Forbidden, NotFound
from easytodo.models import BoardRole


@pytest.mark.parametrize(
    "role,action,allowed",
    [
        (BoardRole.VIEWER, Action.VIEW, True),
        (BoardRole.VIEWER, Action.EDIT_CONTENT, False),
        (BoardRole.EDITOR, Action.EDIT_CONTENT, True),
        (BoardRole.EDITOR, Action.MANAGE_MEMBERS, False),
        (BoardRole.EDITOR, Action.MANAGE_BOARD, False),
        (BoardRole.OWNER, Action.MANAGE_BOARD, True),
    ],
)
def test_permission_matrix(role, action, allowed):
    assert can(role, action) is allowed


ALLOWED_ACTIONS = {
    BoardRole.VIEWER: {Action.VIEW},
    BoardRole.EDITOR: {Action.VIEW, Action.EDIT_CONTENT},
    BoardRole.OWNER: {Action.VIEW, Action.EDIT_CONTENT, Action.MANAGE_MEMBERS, Action.MANAGE_BOARD},
}


@pytest.mark.parametrize("role", list(BoardRole))
@pytest.mark.parametrize("action", list(Action))
def test_every_role_and_action(role, action):
    assert can(role, action) is (action in ALLOWED_ACTIONS[role])


def test_roles_for_lists_the_roles_that_may_do_the_action():
    assert roles_for(Action.VIEW) == (BoardRole.VIEWER, BoardRole.EDITOR, BoardRole.OWNER)
    assert roles_for(Action.EDIT_CONTENT) == (BoardRole.EDITOR, BoardRole.OWNER)
    assert roles_for(Action.MANAGE_MEMBERS) == (BoardRole.OWNER,)
    assert roles_for(Action.MANAGE_BOARD) == (BoardRole.OWNER,)
    for action in Action:
        assert set(roles_for(action)) == {role for role in BoardRole if can(role, action)}


async def test_require_board_returns_the_board_and_the_members_role(db):
    owner = await create_user(db, "owner@example.com")
    editor = await create_user(db, "editor@example.com")
    board = await create_board(db, owner, members={editor: BoardRole.EDITOR})

    found, role = await require_board(db, board.id, editor, Action.EDIT_CONTENT)

    assert (found.id, found.name, role) == (board.id, board.name, BoardRole.EDITOR)
    assert (await require_board(db, board.id, owner, Action.MANAGE_BOARD))[1] == BoardRole.OWNER


async def test_require_board_hides_boards_from_non_members_including_admins(db):
    owner = await create_user(db, "owner@example.com")
    stranger = await create_user(db, "stranger@example.com")
    admin = await create_user(db, "admin@example.com", is_admin=True)
    board = await create_board(db, owner)

    for user in (stranger, admin):
        with pytest.raises(NotFound, match="^Board not found.$"):
            await require_board(db, board.id, user, Action.VIEW)


@pytest.mark.parametrize("board_id", [0, -1, 10**6, 2**63, 2**200])
async def test_require_board_unknown_or_impossible_id_is_not_found(db, board_id):
    owner = await create_user(db, "owner@example.com")
    await create_board(db, owner)
    with pytest.raises(NotFound, match="^Board not found.$"):
        await require_board(db, board_id, owner, Action.VIEW)


async def test_require_board_refuses_a_member_without_the_permission(db):
    owner = await create_user(db, "owner@example.com")
    viewer = await create_user(db, "viewer@example.com")
    board = await create_board(db, owner, members={viewer: BoardRole.VIEWER})

    with pytest.raises(Forbidden, match="^You don't have permission to do that.$"):
        await require_board(db, board.id, viewer, Action.EDIT_CONTENT)


async def test_require_board_finds_active_archived_or_either_board(db):
    owner = await create_user(db, "owner@example.com")
    active = await create_board(db, owner, "Groceries")
    old = await create_board(db, owner, "Old list", archived_at=datetime(2026, 10, 1, tzinfo=UTC))

    for archived, found, missing in ((False, active, old), (True, old, active)):
        board, _ = await require_board(db, found.id, owner, Action.VIEW, archived=archived)
        assert board.id == found.id
        with pytest.raises(NotFound, match="^Board not found.$"):
            await require_board(db, missing.id, owner, Action.VIEW, archived=archived)
    for board in (active, old):
        found, _ = await require_board(db, board.id, owner, Action.VIEW, archived=None)
        assert found.id == board.id
    with pytest.raises(NotFound):  # the default: active boards only
        await require_board(db, old.id, owner, Action.VIEW)


@pytest.mark.parametrize("lock", list(RowLock))
async def test_require_board_with_a_lock_refuses_or_hides_as_without(db, lock):
    owner = await create_user(db, "owner@example.com")
    viewer = await create_user(db, "viewer@example.com")
    stranger = await create_user(db, "stranger@example.com")
    board = await create_board(db, owner, members={viewer: BoardRole.VIEWER})

    found, role = await require_board(db, board.id, owner, Action.MANAGE_BOARD, lock=lock)
    assert (found.id, role) == (board.id, BoardRole.OWNER)
    await db.commit()  # lets the lock go
    with pytest.raises(Forbidden):
        await require_board(db, board.id, viewer, Action.MANAGE_BOARD, lock=lock)
    for user, board_id in ((stranger, board.id), (owner, 10**6), (owner, 0)):
        with pytest.raises(NotFound, match="^Board not found.$"):
            await require_board(db, board_id, user, Action.MANAGE_BOARD, lock=lock)
