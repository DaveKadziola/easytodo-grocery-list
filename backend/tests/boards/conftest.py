"""Sharing fixtures: a board with its owner, a signed-in editor of it, a link invite to it, and
the events the sharing code publishes."""

import httpx
import pytest
from factories import create_board, create_user
from helpers import signed_in

from easytodo.boards import invites, service
from easytodo.boards.invites import create_link_invite
from easytodo.models import Board, BoardMember, BoardRole, User
from easytodo.realtime.events import BoardEvent


@pytest.fixture
async def owner(db) -> User:
    return await create_user(db, "owner@example.com")


@pytest.fixture
async def board(db, owner) -> Board:
    return await create_board(db, owner, "Groceries")


@pytest.fixture
async def editor_client(client_factory, db, board) -> httpx.AsyncClient:
    editor = await create_user(db, "editor@example.com")
    db.add(BoardMember(board_id=board.id, user_id=editor.id, role=BoardRole.EDITOR))
    await db.commit()
    return await signed_in(client_factory, "editor@example.com")


@pytest.fixture
async def invite_token(db, board, owner) -> str:
    """The raw token of a link invite to ``board`` as an editor."""
    raw_token, _ = await create_link_invite(db, board.id, owner, BoardRole.EDITOR)
    return raw_token


@pytest.fixture
def sharing_events(monkeypatch) -> list[BoardEvent]:
    """Every event the member and invite code commits, in order."""
    events: list[BoardEvent] = []

    for module in (service, invites):
        commit_and_publish = module.commit_and_publish

        async def spy(db, *published, _commit_and_publish=commit_and_publish):
            events.extend(published)
            await _commit_and_publish(db, *published)

        monkeypatch.setattr(module, "commit_and_publish", spy)
    return events
