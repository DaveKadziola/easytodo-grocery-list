"""Board-content fixtures: a board with its own owner, and signed-in clients for an editor and a
viewer of that board."""

from collections.abc import Awaitable, Callable

import httpx
import pytest
from factories import create_board, create_category, create_item, create_user
from helpers import login
from sqlalchemy import select

from easytodo.attachments import service as attachments
from easytodo.content import checklist, service
from easytodo.models import (
    Attachment,
    Board,
    BoardMember,
    BoardRole,
    Category,
    ChecklistEntry,
    Item,
    User,
)
from easytodo.realtime.events import BoardEvent, current_origin

SignedInClient = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest.fixture
def signed_in_client(client_factory) -> SignedInClient:
    """``await signed_in_client(email)`` -> a new client signed in as that (existing) user."""

    async def make(email: str) -> httpx.AsyncClient:
        client = await client_factory()
        await login(client, email)
        return client

    return make


@pytest.fixture
async def board(db) -> Board:
    owner = await create_user(db, "owner@example.com")
    return await create_board(db, owner, "Groceries")


@pytest.fixture
async def category(db, board) -> Category:
    return await create_category(db, board, "Dairy")


@pytest.fixture
async def item(db, category) -> Item:
    return await create_item(db, category, "Milk")


async def _member_client(signed_in_client, db, board, email, role) -> httpx.AsyncClient:
    user = await create_user(db, email)
    db.add(BoardMember(board_id=board.id, user_id=user.id, role=role))
    await db.commit()
    return await signed_in_client(email)


@pytest.fixture
async def editor_client(signed_in_client, db, board) -> httpx.AsyncClient:
    return await _member_client(signed_in_client, db, board, "editor@example.com", BoardRole.EDITOR)


@pytest.fixture
async def viewer_client(signed_in_client, db, board) -> httpx.AsyncClient:
    return await _member_client(signed_in_client, db, board, "viewer@example.com", BoardRole.VIEWER)


@pytest.fixture
async def target_board(db, editor_client) -> Board:
    """The board "Weekend" of target-owner@example.com, on which editor@example.com
    (``editor_client``) is an editor: where a category of ``board`` can be moved to."""
    owner = await create_user(db, "target-owner@example.com")
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    return await create_board(db, owner, "Weekend", {editor: BoardRole.EDITOR})


@pytest.fixture
def published(monkeypatch) -> list[tuple[tuple[BoardEvent, ...], str | None]]:
    """Every ``commit_and_publish`` call the content services (categories and items, checklists,
    attachments) make: its events and the request's origin (``X-Client-Id``)."""
    calls: list[tuple[tuple[BoardEvent, ...], str | None]] = []

    def spying_on(commit_and_publish):
        async def spy(db, *events):
            calls.append((events, current_origin.get()))
            await commit_and_publish(db, *events)

        return spy

    for module in (service, checklist, attachments):
        monkeypatch.setattr(module, "commit_and_publish", spying_on(module.commit_and_publish))
    return calls


@pytest.fixture
def content_snapshot(db) -> Callable[[], Awaitable[tuple[list, list, list, list]]]:
    """``await content_snapshot()`` -> every category, item, checklist entry and attachment row,
    to show nothing changed."""

    async def take() -> tuple[list, list, list, list]:
        categories = await db.execute(
            select(
                Category.id,
                Category.board_id,
                Category.name,
                Category.position,
                Category.archived_at,
            ).order_by(Category.id)
        )
        items = await db.execute(
            select(
                Item.id,
                Item.board_id,
                Item.category_id,
                Item.name,
                Item.description,
                Item.is_done,
                Item.done_at,
                Item.deleted_at,
            ).order_by(Item.id)
        )
        entries = await db.execute(
            select(
                ChecklistEntry.id,
                ChecklistEntry.item_id,
                ChecklistEntry.text,
                ChecklistEntry.is_done,
                ChecklistEntry.position,
            ).order_by(ChecklistEntry.id)
        )
        attachments = await db.execute(
            select(
                Attachment.id, Attachment.item_id, Attachment.filename, Attachment.storage_key
            ).order_by(Attachment.id)
        )
        return categories.all(), items.all(), entries.all(), attachments.all()

    return take
