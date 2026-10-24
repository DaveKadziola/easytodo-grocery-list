"""Notification fixtures: a board with its owner, an editor and a viewer, and the events the
notification service publishes."""

import pytest
from factories import create_board, create_user

from easytodo.models import Board, BoardRole, User
from easytodo.notifications import service
from easytodo.realtime.events import BoardEvent, current_origin


@pytest.fixture
async def team(db) -> tuple[Board, User, User, User]:
    """owner@ owns "Groceries"; eve@ ("Eve Adams") is an editor of it, frank@ a viewer. Returns
    ``(board, owner, eve, frank)``."""
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com", display_name="Eve Adams")
    frank = await create_user(db, "frank@example.com")
    board = await create_board(
        db, owner, "Groceries", members={eve: BoardRole.EDITOR, frank: BoardRole.VIEWER}
    )
    return board, owner, eve, frank


@pytest.fixture
def published(monkeypatch) -> list[tuple[tuple[BoardEvent, ...], str | None]]:
    """Every ``commit_and_publish`` call the notification service makes (reading them all): its
    events and the request's origin (``X-Client-Id``). The hooks in the board and category code
    publish through their own modules, which the sharing and content fixtures spy on."""
    calls: list[tuple[tuple[BoardEvent, ...], str | None]] = []
    commit_and_publish = service.commit_and_publish

    async def spy(db, *events):
        calls.append((events, current_origin.get()))
        await commit_and_publish(db, *events)

    monkeypatch.setattr(service, "commit_and_publish", spy)
    return calls
