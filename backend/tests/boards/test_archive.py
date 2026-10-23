"""Archiving a board (its owner only): gone for every member, its data kept, until the owner
restores it from the "Archived boards" page or deletes it there for good by typing its name."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from content.test_isolation import ROUTES, fill
from factories import (
    create_attachment,
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import (
    NASTY,
    NASTY_ESCAPED,
    TYPED_NAMES,
    flash_of,
    signed_in,
    text_of,
    toast_message,
    wait_until_a_query_waits_for_a_lock,
)
from sqlalchemy import delete, select, text, update

from easytodo.boards import routes as board_routes
from easytodo.boards.invite_routes import router as invite_router
from easytodo.boards.invites import INVITE_INVALID, create_link_invite
from easytodo.boards.routes import router as boards_router
from easytodo.boards.service import (
    BOARD_NOT_ARCHIVED,
    CONFIRM_BOARD_NAME,
    count_archived_boards,
    list_boards,
)
from easytodo.models import (
    Attachment,
    Board,
    BoardInvite,
    BoardMember,
    BoardRole,
    Category,
    ChecklistEntry,
    Item,
    User,
)
from easytodo.realtime.events import BoardEvent, EventType

HX = {"HX-Request": "true"}
NO_PERMISSION = "You don't have permission to do that."
BOARD_GONE = "Board not found."
ARCHIVED_AT = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
# The only board routes that reach an archived board.
ARCHIVE_ROUTES = {("POST", "/boards/{board}/restore"), ("DELETE", "/boards/{board}")}


async def rows(db) -> tuple[list, ...]:
    """Every board, membership, invite, category, item, checklist entry and attachment row, to
    show what changed."""
    queries = (
        select(Board.id, Board.name, Board.owner_id, Board.archived_at).order_by(Board.id),
        select(BoardMember.board_id, BoardMember.user_id, BoardMember.role).order_by(
            BoardMember.board_id, BoardMember.user_id
        ),
        select(
            BoardInvite.id, BoardInvite.board_id, BoardInvite.accepted_at, BoardInvite.revoked_at
        ).order_by(BoardInvite.id),
        select(Category.id, Category.board_id, Category.name, Category.position).order_by(
            Category.id
        ),
        select(Item.id, Item.board_id, Item.category_id, Item.name, Item.is_done).order_by(Item.id),
        select(
            ChecklistEntry.id, ChecklistEntry.item_id, ChecklistEntry.text, ChecklistEntry.is_done
        ).order_by(ChecklistEntry.id),
        select(
            Attachment.id, Attachment.item_id, Attachment.filename, Attachment.storage_key
        ).order_by(Attachment.id),
    )
    return tuple([(await db.execute(query)).all() for query in queries])


async def archive(db, board: Board, when: datetime = ARCHIVED_AT) -> None:
    """Archive ``board`` straight in the database (no event)."""
    await db.execute(update(Board).where(Board.id == board.id).values(archived_at=when))
    await db.commit()


async def user_named(db, email: str) -> User:
    return await db.scalar(select(User).where(User.email == email))


def archived_entry(html: str, board_id: int) -> str:
    match = re.search(rf'<li id="archived-board-{board_id}".*?</li>', html, re.DOTALL)
    assert match, f"no archived board {board_id} on the page"
    return match[0]


async def test_owner_archives_board(
    owner, board, editor_client, sharing_events, client_factory, db
):
    dairy = await create_category(db, board, "Dairy")
    await create_item(db, dairy, "Milk")
    owner_client = await signed_in(client_factory, "owner@example.com")
    assert (await owner_client.get(f"/boards/{board.id}")).status_code == 200  # last board shown
    before = await rows(db)

    r = await owner_client.post(f"/boards/{board.id}/archive", headers=HX)

    assert r.status_code == 200
    assert r.headers["hx-redirect"] == "/boards"
    assert flash_of(r) == {"level": "info", "message": "Board archived."}
    archived_at = await db.scalar(select(Board.archived_at))
    assert abs(datetime.now(UTC) - archived_at) < timedelta(minutes=1)
    after = await rows(db)
    assert [row[:3] for row in after[0]] == [row[:3] for row in before[0]]  # only archived_at
    assert after[1:] == before[1:]  # members, invites, categories and items are all kept
    editor = await user_named(db, "editor@example.com")
    for user in (owner, editor):
        assert await list_boards(db, user) == []
    assert sharing_events == [  # the members are told; the owner archived it themself
        BoardEvent(EventType.BOARD_DELETED, board_id=board.id),
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=editor.id),
    ]
    # "/" opened the last board shown on this device (and the user's only one): not any more.
    assert owner_client.cookies["easytodo_last_board"] == str(board.id)
    assert (await owner_client.get("/")).headers["location"] == "/boards"
    assert (await editor_client.get("/")).headers["location"] == "/boards"


async def test_writes_without_htmx_redirect_with_a_flash(owner, board, client_factory, db):
    owner_client = await signed_in(client_factory, "owner@example.com")

    for method, path, headers, location, message in (
        ("POST", f"/boards/{board.id}/archive", {}, "/boards", "Board archived."),
        ("POST", f"/boards/{board.id}/restore", {}, "/boards/archived", "Board restored."),
        ("POST", f"/boards/{board.id}/archive", {}, "/boards", "Board archived."),
        (
            "DELETE",
            f"/boards/{board.id}",
            {"HX-Prompt": "Groceries"},
            "/boards/archived",
            "Board deleted permanently.",
        ),
    ):
        r = await owner_client.request(method, path, headers=headers)
        assert r.status_code == 303, (method, path)
        assert r.headers["location"] == location
        assert flash_of(r) == {"level": "info", "message": message}

    assert await db.scalar(select(Board.id)) is None


async def test_only_the_owner_archives_restores_and_deletes(owner, board, client_factory, db):
    eve = await create_user(db, "eve@example.com")
    frank = await create_user(db, "frank@example.com")
    stranger = await create_user(db, "stranger@example.com")
    await create_user(db, "admin@example.com", is_admin=True)  # admins see only their boards
    await create_board(db, stranger, "Stranger's board")
    members = {eve: BoardRole.EDITOR, frank: BoardRole.VIEWER}
    for user, role in members.items():
        db.add(BoardMember(board_id=board.id, user_id=user.id, role=role))
    await db.commit()
    old = await create_board(db, owner, "Old list", members, archived_at=ARCHIVED_AT)
    await create_item(db, await create_category(db, old, "Dairy"), "Milk")
    before = await rows(db)

    def requests(active_id: str, archived_id: str) -> list[tuple[str, str]]:
        return [
            ("POST", f"/boards/{active_id}/archive"),
            ("POST", f"/boards/{archived_id}/restore"),
            ("DELETE", f"/boards/{archived_id}"),
        ]

    prompt = HX | {"HX-Prompt": "Old list"}
    for email in ("eve@example.com", "frank@example.com"):
        member = await signed_in(client_factory, email)
        for method, path in requests(str(board.id), str(old.id)):
            r = await member.request(method, path, headers=prompt)
            assert r.status_code == 403, (email, method, path)
            assert toast_message(r) == NO_PERMISSION
    for email in ("stranger@example.com", "admin@example.com"):
        outsider = await signed_in(client_factory, email)
        for method, path in requests(str(board.id), str(old.id)):
            r = await outsider.request(method, path, headers=prompt)
            assert r.status_code == 404, (email, method, path)
            assert json.loads(r.headers["hx-trigger"]) == {
                "toast": {"level": "error", "message": BOARD_GONE},
                "resync": True,
            }
    owner_client = await signed_in(client_factory, "owner@example.com")
    for bad_id in ("999999", "0", "-1", str(2**63)):
        for method, path in requests(bad_id, bad_id):
            r = await owner_client.request(method, path, headers=prompt)
            assert r.status_code == 404, (method, path)

    assert await rows(db) == before


async def test_archived_board_is_404_everywhere(  # Review Focus 1
    owner, board, editor_client, invite_token, client_factory, db
):
    dairy = await create_category(db, board, "Dairy", position=0)
    bakery = await create_category(db, board, "Bakery", position=1)
    milk = await create_item(db, dairy, "Milk")
    entry = await create_checklist_entry(db, milk, "Skim")
    # No file in the store: the answer is 404 before the file is looked for.
    attachment = await create_attachment(db, milk)
    editor = await user_named(db, "editor@example.com")
    invite_id = await db.scalar(select(BoardInvite.id))
    owner_client = await signed_in(client_factory, "owner@example.com")
    await create_user(db, "stranger@example.com")
    stranger = await signed_in(client_factory, "stranger@example.com")
    r = await owner_client.post(f"/boards/{board.id}/archive", headers=HX)
    assert r.status_code == 200
    before = await rows(db)

    ids = {
        "board": board.id,
        "category": dairy.id,
        "other_category": bakery.id,
        "item": milk.id,
        "entry": entry.id,
        "attachment": attachment.id,
    }
    requests = [fill(route, **ids) for route in ROUTES]
    form = {"name": "Hijacked", "role": "viewer"}  # a role, or the sharing routes answer 422
    board_routes_checked = set()
    for route in [*boards_router.routes, *invite_router.routes]:
        path = route.path.replace("_id}", "}")
        for method in route.methods:
            if path.startswith("/boards/{board}") and (method, path) not in ARCHIVE_ROUTES:
                board_routes_checked.add((method, path))
                url = path.format(board=board.id, invite=invite_id, user=editor.id)
                requests.append((method, url, None if method == "GET" else form))
    assert {
        ("GET", "/boards/{board}"),
        ("PATCH", "/boards/{board}"),
        ("POST", "/boards/{board}/archive"),
        ("GET", "/boards/{board}/members"),
        ("POST", "/boards/{board}/invites"),
        ("DELETE", "/boards/{board}/invites/{invite}"),
        ("PATCH", "/boards/{board}/members/{user}"),
        ("DELETE", "/boards/{board}/members/{user}"),
        ("POST", "/boards/{board}/leave"),
    } <= board_routes_checked

    for client in (owner_client, editor_client):
        for method, path, data in requests:
            r = await client.request(method, path, data=data, headers=HX)
            assert r.status_code == 404, (method, path)
            assert json.loads(r.headers["hx-trigger"])["resync"] is True, (method, path)
    for method, path in (
        ("GET", f"/invite/{invite_token}"),
        ("POST", f"/invite/{invite_token}/accept"),
    ):
        r = await stranger.request(method, path)
        assert r.status_code == 404, method
        assert INVITE_INVALID in r.text and "Groceries" not in r.text

    assert await rows(db) == before


async def test_restore_brings_everything_back(
    owner, board, editor_client, invite_token, sharing_events, client_factory, db
):
    await create_item(db, await create_category(db, board, "Dairy"), "Milk")
    await archive(db, board)
    owner_client = await signed_in(client_factory, "owner@example.com")
    await create_user(db, "bob@example.com")
    bob = await signed_in(client_factory, "bob@example.com")
    assert (await editor_client.get(f"/boards/{board.id}")).status_code == 404
    assert (await bob.get(f"/invite/{invite_token}")).status_code == 404

    r = await owner_client.post(f"/boards/{board.id}/restore", headers=HX)

    assert r.status_code == 200
    assert r.headers["hx-refresh"] == "true"
    assert flash_of(r) == {"level": "info", "message": "Board restored."}
    assert await db.scalar(select(Board.archived_at)) is None
    page = await editor_client.get(f"/boards/{board.id}")
    assert page.status_code == 200 and "Milk" in page.text
    assert [s.board.id for s in await list_boards(db, owner)] == [board.id]
    assert (await bob.get(f"/invite/{invite_token}")).status_code == 200  # the invite works again
    r = await owner_client.post(f"/boards/{board.id}/restore", headers=HX)
    assert r.status_code == 404  # only an archived board is restored
    assert toast_message(r) == BOARD_GONE
    # Nobody can have an archived board open: only the bell has news, for the members.
    editor = await user_named(db, "editor@example.com")
    assert sharing_events == [BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=editor.id)]


async def test_delete_needs_an_archived_board_and_its_typed_name(owner, board, client_factory, db):
    await create_item(db, await create_category(db, board, "Dairy"), "Milk")
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.delete(f"/boards/{board.id}", headers=HX | {"HX-Prompt": "Groceries"})
    assert r.status_code == 409
    assert toast_message(r) == BOARD_NOT_ARCHIVED

    await archive(db, board)
    before = await rows(db)
    for prompt in ({}, {"HX-Prompt": ""}, {"HX-Prompt": "groceries"}, {"HX-Prompt": "Groceries!"}):
        r = await owner_client.delete(f"/boards/{board.id}", headers=HX | prompt)
        assert r.status_code == 422, prompt
        assert toast_message(r) == CONFIRM_BOARD_NAME
    assert await rows(db) == before


@pytest.mark.parametrize(("name", "prompt"), TYPED_NAMES)  # Review Focus 3
async def test_permanent_delete_accepts_a_non_latin1_name(
    owner, client_factory, db, sharing_events, name, prompt
):
    eve = await create_user(db, "eve@example.com")
    doomed = await create_board(db, owner, name, members={eve: BoardRole.EDITOR})
    await create_item(db, await create_category(db, doomed, "Dairy"), "Milk")
    await create_link_invite(db, doomed.id, owner, BoardRole.VIEWER)
    await archive(db, doomed)
    keep = await create_board(db, owner, "Keep me", members={eve: BoardRole.VIEWER})
    await create_item(db, await create_category(db, keep, "Tools"), "Hammer")
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.delete(f"/boards/{doomed.id}", headers=HX | prompt)

    assert r.status_code == 200, r.headers.get("hx-trigger")
    assert r.headers["hx-refresh"] == "true"
    assert flash_of(r) == {"level": "info", "message": "Board deleted permanently."}
    assert list(await db.scalars(select(Board.name))) == ["Keep me"]
    assert set(await db.scalars(select(BoardMember.board_id))) == {keep.id}
    assert list(await db.scalars(select(BoardInvite.id))) == []
    assert list(await db.scalars(select(Category.name))) == ["Tools"]
    assert list(await db.scalars(select(Item.name))) == ["Hammer"]
    assert sharing_events == []


@pytest.mark.parametrize(
    ("method", "path", "archived", "headers", "data"),
    [
        ("PATCH", "/boards/{id}", False, {}, {"name": "Weekend"}),
        ("POST", "/boards/{id}/archive", False, {}, None),
        ("POST", "/boards/{id}/restore", True, {}, None),
        ("DELETE", "/boards/{id}", True, {"HX-Prompt": "Groceries"}, None),
    ],
)
async def test_board_write_racing_a_delete_is_404_not_500(
    owner, board, client_factory, db, method, path, archived, headers, data
):
    """Another device deletes the board for good while the write waits for its row lock: the
    write then finds no board."""
    if archived:
        await archive(db, board)
    owner_client = await signed_in(client_factory, "owner@example.com")

    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(delete(Board).where(Board.id == board.id))
        write = asyncio.create_task(
            owner_client.request(method, path.format(id=board.id), data=data, headers=HX | headers)
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await write

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": BOARD_GONE},
        "resync": True,
    }


@pytest.mark.parametrize(
    ("method", "path", "archived"),
    [
        ("PATCH", "/boards/{id}", False),
        ("POST", "/boards/{id}/archive", False),
        ("POST", "/boards/{id}/restore", True),
    ],
)
async def test_board_changes_do_not_wait_for_a_key_share_lock(
    owner, board, client_factory, db, method, path, archived
):
    """An item being added on another device holds the board FOR KEY SHARE (its foreign key
    check) until it commits: renaming, archiving or restoring the board changes no key, so it
    does not wait."""
    if archived:
        await archive(db, board)
    owner_client = await signed_in(client_factory, "owner@example.com")
    data = {"name": "Weekend"} if method == "PATCH" else None

    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text("SELECT 1 FROM boards WHERE id = :id FOR KEY SHARE"), {"id": board.id}
        )
        request = owner_client.request(method, path.format(id=board.id), data=data, headers=HX)
        r = await asyncio.wait_for(request, 2)

    assert r.status_code == 200, r.headers.get("hx-trigger")


async def test_archived_page_lists_own_archived_boards_with_purge_dates(
    client_factory, db, monkeypatch
):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    await create_user(db, "frank@example.com")
    older = await create_board(db, dave, "Old list", archived_at=datetime(2026, 9, 2, tzinfo=UTC))
    newer = await create_board(db, dave, NASTY, archived_at=ARCHIVED_AT)
    await create_board(db, dave, "Groceries")  # active
    # Archived by its owner Eve, with Dave as an editor: Eve's archive, not Dave's.
    await create_board(db, eve, "Eve's plans", {dave: BoardRole.EDITOR}, archived_at=ARCHIVED_AT)
    contexts: list[dict] = []
    render = board_routes.render

    def render_spy(request, name, context, *args, **kwargs):
        contexts.append(context)
        return render(request, name, context, *args, **kwargs)

    monkeypatch.setattr(board_routes, "render", render_spy)
    client = await signed_in(client_factory, "dave@example.com")

    html = (await client.get("/boards/archived")).text

    assert re.search(r"<h1[^>]*>Archived boards</h1>", html)
    assert 'href="/boards"' in html and "Back to boards" in html
    assert re.findall(r'<li id="archived-board-(\d+)"', html) == [str(newer.id), str(older.id)]
    entry = archived_entry(html, newer.id)
    assert re.search(rf"<h2[^>]*>{re.escape(NASTY_ESCAPED)}</h2>", entry)
    assert '<time datetime="2026-10-01T09:30:00+00:00"' in entry
    assert '<time datetime="2026-10-31T09:30:00+00:00"' in entry
    assert "Archived 2026-10-01 09:30 UTC" in text_of(entry)
    assert "Deleted permanently on 2026-10-31 09:30 UTC" in text_of(entry)  # 30 days
    assert f'hx-post="/boards/{newer.id}/restore"' in entry and ">Restore</button>" in entry
    assert re.search(rf'<button [^>]*class="btn-danger"[^>]*hx-delete="/boards/{newer.id}"', entry)
    assert (
        f'hx-prompt="Type “{NASTY_ESCAPED}” to delete this board and everything on it permanently."'
    ) in entry
    assert "Deleted permanently on 2026-10-02 00:00 UTC" in text_of(archived_entry(html, older.id))
    assert "<b>" not in html and "<script>alert" not in html
    assert "Eve&#39;s plans" not in html and "Groceries" not in html
    assert await count_archived_boards(db, dave) == 2
    await client.get("/boards")
    await client.post("/boards", data={"name": ""})  # 422: the list again
    assert [c["archived_count"] for c in contexts if "boards" in c] == [2, 2]

    await db.execute(update(User).where(User.id == dave.id).values(archive_retention_days=None))
    await db.commit()
    html = (await client.get("/boards/archived")).text
    assert "Kept until you delete it" in text_of(archived_entry(html, newer.id))
    assert "Deleted permanently on" not in html

    html = (
        await (await signed_in(client_factory, "frank@example.com")).get("/boards/archived")
    ).text
    assert "No archived boards." in html and "archived-board-" not in html
