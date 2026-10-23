import json
import re
from datetime import UTC, datetime

import pytest
from factories import DEFAULT_PASSWORD, create_board, create_category, create_item, create_user
from helpers import NASTY, NASTY_ESCAPED, count_queries, login
from sqlalchemy import func, select

from easytodo.boards import service
from easytodo.boards.permissions import Action
from easytodo.boards.service import get_board_view, list_boards
from easytodo.models import Board, BoardMember, BoardRole, User
from easytodo.realtime.events import BoardEvent, EventType, current_origin
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER

HX = {"HX-Request": "true"}
NAME_ERROR = "Board name must be 1–80 characters."
NO_PERMISSION = "You don't have permission to do that."


async def client_for(client_factory, email: str):
    client = await client_factory()
    await login(client, email)
    return client


async def count_rows(db, model) -> int:
    return await db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def published(monkeypatch) -> list[tuple[tuple[BoardEvent, ...], str | None]]:
    """Every ``commit_and_publish`` call the board service makes: its events and the request's
    origin (``X-Client-Id``)."""
    calls: list[tuple[tuple[BoardEvent, ...], str | None]] = []
    commit_and_publish = service.commit_and_publish

    async def spy(db, *events):
        calls.append((events, current_origin.get()))
        await commit_and_publish(db, *events)

    monkeypatch.setattr(service, "commit_and_publish", spy)
    return calls


async def test_registration_creates_default_board(client, db):
    await client.get("/register")
    r = await client.post(
        "/register",
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 303

    user_id = await db.scalar(select(User.id))
    board = await db.scalar(select(Board))
    assert (board.name, board.owner_id) == ("My list", user_id)
    members = (
        await db.execute(select(BoardMember.board_id, BoardMember.user_id, BoardMember.role))
    ).all()
    assert members == [(board.id, user_id, BoardRole.OWNER)]
    page = await client.get("/boards")
    assert page.status_code == 200
    assert "My list" in page.text


async def test_failed_registration_creates_no_board(client, db):
    await create_user(db, "dave@example.com")
    await client.get("/register")
    r = await client.post(
        "/register",
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 400
    assert await count_rows(db, Board) == 0
    assert await count_rows(db, User) == 1


async def test_root_redirects_to_boards(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/boards"


@pytest.mark.parametrize("path", ["/boards", "/boards/1", "/boards/archived"])
async def test_board_pages_require_sign_in(client, path):
    r = await client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=" + path.replace("/", "%2F")


async def test_user_sees_only_own_and_shared_boards(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    mallory = await create_user(db, "mallory@example.com")
    groceries = await create_board(db, dave, "Groceries")
    hardware = await create_board(db, eve, "Hardware store", members={dave: BoardRole.VIEWER})
    await create_board(db, eve, "Eve private plans")
    await create_board(db, mallory, "Mallory board")
    dairy = await create_category(db, groceries, "Dairy")
    await create_item(db, dairy, "Milk")
    await create_item(db, dairy, "Butter")
    await create_item(db, dairy, "Cheese", is_done=True)
    client = await client_for(client_factory, "dave@example.com")

    r = await client.get("/boards")

    assert r.status_code == 200
    assert re.findall(r'href="/boards/(\d+)"', r.text) == [str(groceries.id), str(hardware.id)]
    assert "Groceries" in r.text and "Hardware store" in r.text
    assert "Eve private plans" not in r.text and "Mallory board" not in r.text
    assert "2 open items" in r.text and "0 open items" in r.text
    assert "Owner: eve@example.com" in r.text and "Viewer" in r.text
    assert 'action="/boards"' in r.text and "New board" in r.text


async def test_list_boards_puts_own_boards_first_then_sorts_by_name_in_one_query(db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    frank = await create_user(db, "frank@example.com")
    await create_board(db, dave, "Cherry list")
    await create_board(db, dave, "banana list")
    club = await create_board(
        db, eve, "Apple club", members={dave: BoardRole.EDITOR, frank: BoardRole.VIEWER}
    )
    await create_board(db, eve, "Eve private plans")
    await create_board(db, dave, "Archived list", archived_at=datetime(2026, 10, 1, tzinfo=UTC))
    await create_board(
        db, eve, "Archived club", {dave: BoardRole.EDITOR}, archived_at=datetime.now(UTC)
    )
    bread = await create_category(db, club, "Bread")
    await create_item(db, bread, "Rye")
    await create_item(db, bread, "Baguette")
    await create_item(db, bread, "Stale loaf", is_done=True)
    old_bread = await create_category(db, club, "Old bread", archived_at=datetime.now(UTC))
    await create_item(db, old_bread, "Archived loaf")  # open, but its category is archived

    with count_queries(db) as statements:
        summaries = await list_boards(db, dave)

    assert len(statements) == 1
    assert [
        (s.board.name, s.role, s.member_count, s.open_items, s.owner_email) for s in summaries
    ] == [
        ("banana list", BoardRole.OWNER, 1, 0, "dave@example.com"),
        ("Cherry list", BoardRole.OWNER, 1, 0, "dave@example.com"),
        ("Apple club", BoardRole.EDITOR, 3, 2, "eve@example.com"),
    ]


async def test_create_board_redirects_to_it_with_the_creator_as_owner(client, db):
    dave = await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")

    r = await client.post("/boards", data={"name": "  Hardware store  "})

    board = await db.scalar(select(Board))
    assert r.status_code == 303
    assert r.headers["location"] == f"/boards/{board.id}"
    assert (board.name, board.owner_id) == ("Hardware store", dave.id)
    members = (await db.execute(select(BoardMember.user_id, BoardMember.role))).all()
    assert members == [(dave.id, BoardRole.OWNER)]
    page = await client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Hardware store" in page.text


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
async def test_board_name_validation(client, db, name):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")

    r = await client.post("/boards", data={"name": name})

    assert r.status_code == 422
    assert NAME_ERROR in r.text
    assert 'action="/boards"' in r.text  # the list page again, with the form
    assert await count_rows(db, Board) == 0


async def test_board_name_may_have_80_characters_after_stripping(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    r = await client.post("/boards", data={"name": "  " + "x" * 80 + "  "})
    assert r.status_code == 303
    assert await db.scalar(select(Board.name)) == "x" * 80


async def test_non_member_gets_404_for_board(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    await create_user(db, "eve@example.com")
    await create_user(db, "admin@example.com", is_admin=True)  # admins see only their boards too
    board = await create_board(db, dave, "Groceries")

    for email in ("eve@example.com", "admin@example.com"):
        outsider = await client_for(client_factory, email)

        r = await outsider.get(f"/boards/{board.id}")
        assert r.status_code == 404
        assert "Board not found." in r.text
        assert "Groceries" not in r.text

        r = await outsider.patch(f"/boards/{board.id}", data={"name": "Taken over"}, headers=HX)
        assert r.status_code == 404
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": "Board not found."},
            "resync": True,
        }

        r = await outsider.delete(f"/boards/{board.id}", headers=HX)
        assert r.status_code == 404

    assert await db.scalar(select(Board.name)) == "Groceries"


@pytest.mark.parametrize("board_id", ["999999", "0", str(2**63)])
async def test_unknown_board_is_404(client, db, board_id):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    r = await client.get(f"/boards/{board_id}")
    assert r.status_code == 404
    assert "Board not found." in r.text


async def test_editor_cannot_rename_or_delete_board(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    frank = await create_user(db, "frank@example.com")
    board = await create_board(
        db, dave, "Groceries", members={eve: BoardRole.EDITOR, frank: BoardRole.VIEWER}
    )

    for email in ("eve@example.com", "frank@example.com"):
        member = await client_for(client_factory, email)

        page = await member.get(f"/boards/{board.id}")
        assert page.status_code == 200
        assert f'hx-patch="/boards/{board.id}"' not in page.text  # no owner controls
        assert f'hx-delete="/boards/{board.id}"' not in page.text

        r = await member.patch(f"/boards/{board.id}", data={"name": "Renamed"}, headers=HX)
        assert r.status_code == 403
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": NO_PERMISSION}
        }

        r = await member.delete(f"/boards/{board.id}", headers=HX)
        assert r.status_code == 403

    assert await db.scalar(select(Board.name)) == "Groceries"


async def test_owner_renames_board(client, db, published):
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave, "Groceries")
    await login(client, "dave@example.com")

    page = await client.get("/boards")  # the rename form is in the board's ⋮ menu on the list
    assert f'hx-patch="/boards/{board.id}"' in page.text

    r = await client.patch(
        f"/boards/{board.id}",
        data={"name": " Weekly shop "},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert r.headers["hx-refresh"] == "true"
    assert await db.scalar(select(Board.name)) == "Weekly shop"
    assert published == [((BoardEvent(EventType.BOARD_UPDATED, board_id=board.id),), "tab-1")]

    r = await client.patch(f"/boards/{board.id}", data={"name": "Without htmx"})

    assert r.status_code == 303
    assert r.headers["location"] == "/boards"
    assert await db.scalar(select(Board.name)) == "Without htmx"


@pytest.mark.parametrize("name", ["", "x" * 81])
async def test_rename_validates_the_name(client, db, name):
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave, "Groceries")
    await login(client, "dave@example.com")

    r = await client.patch(f"/boards/{board.id}", data={"name": name}, headers=HX)

    assert r.status_code == 422
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": NAME_ERROR}
    }
    assert await db.scalar(select(Board.name)) == "Groceries"


@pytest.mark.parametrize("breach", ["no_token", "foreign_origin"])
@pytest.mark.parametrize(
    "method,path,data,archived",
    [
        ("POST", "/boards", {"name": "New one"}, False),
        ("PATCH", "/boards/{id}", {"name": "Renamed"}, False),
        ("POST", "/boards/{id}/archive", None, False),
        ("POST", "/boards/{id}/restore", None, True),
        ("DELETE", "/boards/{id}", None, True),
    ],
)
async def test_board_writes_require_the_csrf_token(
    client, db, method, path, data, archived, breach
):
    dave = await create_user(db, "dave@example.com")
    archived_at = datetime(2026, 10, 1, tzinfo=UTC) if archived else None
    board = await create_board(db, dave, "Groceries", archived_at=archived_at)
    await login(client, "dave@example.com")
    if breach == "no_token":
        del client.headers[CSRF_HEADER]
    else:
        client.headers["Origin"] = "https://evil.example"

    r = await client.request(
        method, path.format(id=board.id), data=data, headers={"HX-Prompt": "Groceries"}
    )

    assert r.status_code == 403
    rows = await db.execute(select(Board.name, Board.archived_at))
    assert rows.all() == [("Groceries", archived_at)]


async def test_board_page_lists_categories_and_items(client, db):
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave, "Groceries")
    dairy = await create_category(db, board, "Dairy", position=0)
    await create_item(db, dairy, "Milk")
    await create_item(db, await create_category(db, board, "Bakery", position=1), "Bread")
    await login(client, "dave@example.com")

    r = await client.get(f"/boards/{board.id}")

    assert r.status_code == 200
    for text in ("Groceries", "Dairy", "Milk", "Bakery", "Bread"):
        assert text in r.text
    assert r.text.index("Dairy") < r.text.index("Bakery")


async def test_board_names_are_rendered_escaped(client, db):  # Review Focus 1
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave, NASTY)
    await login(client, "dave@example.com")

    for path in ("/boards", f"/boards/{board.id}"):
        html = (await client.get(path)).text
        assert "<script>alert(1)</script>" not in html and "<b>" not in html
        assert NASTY_ESCAPED in html


async def test_board_view_uses_two_queries(db, monkeypatch):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner)
    dairy = await create_category(db, board, "Dairy", position=1)
    fruit = await create_category(db, board, "Fruit", position=0)
    await create_category(db, board, "Empty", position=2)
    await create_item(db, dairy, "milk")
    await create_item(db, fruit, "banana", is_done=True)
    await create_item(db, fruit, "Apple")
    await create_item(db, fruit, "cherry")
    archived = await create_category(db, board, "Archived", archived_at=datetime.now(UTC))
    await create_item(db, archived, "Not on the board any more")
    elsewhere = await create_category(db, await create_board(db, owner, "Other"), "Elsewhere")
    await create_item(db, elsewhere, "Not on this board")

    authorized: list[Action] = []
    require_board = service.require_board

    with count_queries(db) as statements:

        async def require_board_then_count(db, board_id, user, action):
            result = await require_board(db, board_id, user, action)
            authorized.append(action)
            statements.clear()  # count only what runs after authorization
            return result

        monkeypatch.setattr(service, "require_board", require_board_then_count)
        view = await get_board_view(db, board.id, owner)

    assert authorized == [Action.VIEW]
    assert len(statements) == 2
    assert (view.board.id, view.role) == (board.id, BoardRole.OWNER)
    assert [(c.category.name, [i.item.name for i in c.items]) for c in view.categories] == [
        ("Fruit", ["Apple", "cherry", "banana"]),  # open first, then by name ignoring case
        ("Dairy", ["milk"]),
        ("Empty", []),
    ]


async def test_home_opens_the_last_board_or_the_only_one(client, db):
    """Opening the app ("/", the PWA's start URL) goes straight to a board where it can: the one
    last opened on this device, else the user's only board; the board list otherwise."""
    dave = await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    assert (await client.get("/")).headers["location"] == "/boards"  # no boards yet

    only = await create_board(db, dave, "Groceries")
    assert (await client.get("/")).headers["location"] == f"/boards/{only.id}"

    other = await create_board(db, dave, "Weekend")
    assert (await client.get("/")).headers["location"] == "/boards"  # which one is unclear
    assert (await client.get(f"/boards/{other.id}")).status_code == 200
    assert (await client.get("/")).headers["location"] == f"/boards/{other.id}"

    stranger = await create_user(db, "eve@example.com")
    foreign = await create_board(db, stranger, "Eve's")
    client.cookies.set("easytodo_last_board", str(foreign.id))
    assert (await client.get("/")).headers["location"] == "/boards"  # not a member: ignored
    client.cookies.set("easytodo_last_board", "not-a-number")
    assert (await client.get("/")).headers["location"] == "/boards"


@pytest.mark.parametrize("name", ["a\x00b", "\x00"])
async def test_board_name_with_nul_is_cleaned_not_a_500(client, db, name):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")

    r = await client.post("/boards", data={"name": name, "csrf_token": client.cookies[CSRF_COOKIE]})

    if name == "\x00":
        assert r.status_code == 422 and NAME_ERROR in r.text
    else:
        assert r.status_code == 303
        board = (await db.scalars(select(Board))).one()
        assert board.name == "ab"
        r = await client.patch(f"/boards/{board.id}", data={"name": "x\x00y"}, headers=HX)
        assert r.status_code == 200
        await db.refresh(board)
        assert board.name == "xy"
