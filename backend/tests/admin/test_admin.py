"""The admin panel: statistics, the user list, creating accounts with a temporary password,
deleting, disabling, password reset mails and resetting 2FA. Only admins see it, and being an
admin opens nobody else's boards."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
import pytest
from factories import (
    DEFAULT_PASSWORD,
    create_board,
    create_category,
    create_item,
    create_user,
)
from helpers import login, signed_in, toast_message
from sqlalchemy import func, select, update
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from easytodo.admin import service
from easytodo.admin.routes import router
from easytodo.admin.service import AdminStats, delete_user, get_stats, list_users
from easytodo.auth import deps
from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.auth.twofa import begin_setup, confirm_setup
from easytodo.errors import Conflict
from easytodo.models import (
    Board,
    BoardMember,
    BoardRole,
    Category,
    Item,
    RecoveryCode,
    User,
    UserSession,
    UserStatus,
)
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_COOKIE
from easytodo.security.passwords import verify_password
from easytodo.web import FLASH_COOKIE

HX = {"HX-Request": "true"}
CANNOT_DELETE_SELF = "You can't delete your own account here."
LAST_ADMIN = "At least one active admin is required."
TEMP_PASSWORD_ON_PAGE = re.compile(r'id="temp-password" value="([^"]+)"')
USER_ROWS = re.compile(r'<tr id="user-(\d+)"')
RESET_LINK = re.compile(r"/password/reset/(\S+)")

# Every admin route, as (method, path template, form data); {user} is filled in per test.
ROUTES = [
    ("GET", "/admin", None),
    ("GET", "/admin/users", None),
    ("POST", "/admin/users", {"email": "new@example.com"}),
    ("DELETE", "/admin/users/{user}", None),
    ("POST", "/admin/users/{user}/send-reset", None),
    ("POST", "/admin/users/{user}/status", {"status": "disabled"}),
    ("POST", "/admin/users/{user}/reset-2fa", None),
]


def listed_user_ids(html: str) -> list[int]:
    return [int(user_id) for user_id in USER_ROWS.findall(html)]


async def fresh(db, user: User) -> User:
    """``user`` as the database has it now (the app changed it through its own session)."""
    await db.refresh(user)
    return user


async def session_ids(db, user: User) -> list[int]:
    return list(await db.scalars(select(UserSession.id).where(UserSession.user_id == user.id)))


def test_routes_cover_every_admin_route():
    registered = {
        (method, route.path.replace("user_id}", "user}"))
        for route in router.routes
        for method in route.methods
    }
    assert registered == {(method, path) for method, path, _ in ROUTES}


async def test_non_admin_gets_404_on_admin_pages(client, db, mail_outbox):
    await create_user(db, "dave@example.com")
    other = await create_user(db, "other@example.com")
    await login(client, "dave@example.com")

    for method, path, data in ROUTES:
        r = await client.request(method, path.format(user=other.id), data=data)
        assert r.status_code == 404, (method, path)
        assert "Not found." in r.text

    assert (await fresh(db, other)).status == UserStatus.ACTIVE
    assert await db.scalar(select(func.count()).select_from(User)) == 2  # nobody added or deleted
    assert mail_outbox == []


async def test_anonymous_user_is_sent_to_login(client):
    r = await client.get("/admin/users")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fadmin%2Fusers"


async def test_admin_link_shows_only_for_admins(admin_client, client, db):
    assert 'href="/admin"' in (await admin_client.get("/boards")).text

    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    assert 'href="/admin"' not in (await client.get("/boards")).text


async def test_stats_counts(admin_client, db, admin):
    now = datetime.now(UTC)
    recent = await create_user(db, "recent@example.com")
    idle = await create_user(db, "idle@example.com")
    # The admin signed in just now; the others 10 and 40 days ago. One has 2FA on.
    await db.execute(
        update(User)
        .where(User.id == recent.id)
        .values(last_login_at=now - timedelta(days=10), totp_enabled=True)
    )
    await db.execute(
        update(User).where(User.id == idle.id).values(last_login_at=now - timedelta(days=40))
    )
    await db.commit()
    shared = await create_board(db, recent, "Groceries", {idle: BoardRole.EDITOR})
    own = await create_board(db, admin, "Chores")
    dairy = await create_category(db, shared, "Dairy")
    bakery = await create_category(db, shared, "Bakery", position=1)
    garden = await create_category(db, own, "Garden")
    await create_item(db, dairy, "Milk")
    await create_item(db, dairy, "Butter")
    await create_item(db, bakery, "Bread", is_done=True)
    await create_item(db, garden, "Mow the lawn")

    expected = AdminStats(
        users_total=3,
        users_active_30d=2,
        users_with_2fa=1,
        boards_total=2,
        shared_boards=1,
        categories_total=3,
        items_total=4,
        items_open=3,
    )
    assert await get_stats(db) == expected

    r = await admin_client.get("/admin")
    assert r.status_code == 200
    shown = dict(re.findall(r"<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>(\d+)</dd>", r.text))
    assert shown == {
        "Users": "3",
        "Active in the last 30 days": "2",
        "With two-factor authentication": "1",
        "Boards": "2",
        "Shared boards": "1",
        "Categories": "3",
        "Items": "4",
        "Open items": "3",
    }


async def test_user_list_shows_every_user_with_details(admin_client, db, admin):
    dave = await create_user(db, "dave@example.com", status=UserStatus.DISABLED)
    await create_board(db, dave, "One")
    await create_board(db, dave, "Two")

    r = await admin_client.get("/admin/users")

    assert r.status_code == 200
    assert listed_user_ids(r.text) == [admin.id, dave.id]  # by e-mail
    row = r.text[r.text.index(f'<tr id="user-{dave.id}"') :]
    row = row[: row.index("</tr>")]
    assert "dave@example.com" in row
    assert "Disabled" in row
    assert "Never" in row  # never signed in
    # Deleting asks first, and says how many boards go with the account.
    assert "Delete dave@example.com and the 2 boards they own?" in row


async def test_user_search_escapes_wildcards(admin_client, db, admin):
    underscore = await create_user(db, "a_b@example.com")
    lookalike = await create_user(db, "axb@example.com")
    percent = await create_user(db, "100%sure@example.com")
    slash = await create_user(db, "a/b@example.com")
    everyone = sorted(u.id for u in (admin, underscore, lookalike, percent, slash))

    async def search(q: str) -> list[int]:
        r = await admin_client.get("/admin/users", params={"q": q})
        assert r.status_code == 200
        return sorted(listed_user_ids(r.text))

    assert await search("%") == [percent.id]  # not everyone
    assert await search("_") == [underscore.id]  # not every e-mail with a character there
    assert await search("a/b") == [slash.id]
    assert await search("AXB") == [lookalike.id]  # ignoring case
    assert await search("  ") == everyone
    assert await search("") == everyone
    assert 'value="100%"' in (await admin_client.get("/admin/users?q=100%25")).text


async def test_user_list_pages(db, admin):
    for n in range(4):
        await create_user(db, f"user{n}@example.com")

    page = await list_users(db, q=None, page=2, per_page=2)  # after admin@ and user0@
    assert [row.email for row in page.items] == ["user1@example.com", "user2@example.com"]
    assert (page.total, page.page, page.per_page, page.pages) == (5, 2, 2, 3)

    # Past the end shows the last page; before the start, the first.
    assert [row.email for row in (await list_users(db, q=None, page=9, per_page=2)).items] == [
        "user3@example.com"
    ]
    assert (await list_users(db, q=None, page=0, per_page=2)).page == 1
    assert (await list_users(db, q="user", page=1)).total == 4


async def test_user_list_links_to_the_other_page_with_the_search(admin_client, db):
    db.add_all(User(email=f"user{n:02}@example.com", password_hash="-") for n in range(51))
    await db.commit()

    r = await admin_client.get("/admin/users", params={"q": "user"})
    assert len(listed_user_ids(r.text)) == 50
    assert "Page 1 of 2" in r.text
    assert 'href="/admin/users?q=user&amp;page=2"' in r.text

    r = await admin_client.get("/admin/users", params={"q": "user", "page": "2"})
    assert len(listed_user_ids(r.text)) == 1
    assert "Page 2 of 2" in r.text
    assert 'href="/admin/users?q=user&amp;page=1"' in r.text


async def test_created_user_must_change_password(admin_client, client_factory, db):
    r = await admin_client.post("/admin/users", data={"email": " New@Example.COM "})

    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    password = TEMP_PASSWORD_ON_PAGE.search(r.text)[1]
    assert len(password) == 16  # secrets.token_urlsafe(12)
    user = await db.scalar(select(User).where(User.email == "new@example.com"))
    assert user.must_change_password is True
    assert user.is_admin is False
    assert verify_password(user.password_hash, password)
    assert password not in (await admin_client.get("/admin/users")).text  # shown only once

    newcomer = await client_factory()
    await login(newcomer, "new@example.com", password)
    r = await newcomer.get("/boards")
    assert r.status_code == 303
    assert r.headers["location"] == "/account/password"


async def test_forced_change_refuses_the_temporary_password(client_factory, db):
    user, password = await service.create_user_with_temp_password(db, "new@example.com")
    newcomer = await client_factory()
    await login(newcomer, "new@example.com", password)

    def change(new: str) -> dict[str, str]:
        return {"current_password": password, "new_password": new, "new_password_confirm": new}

    r = await newcomer.post("/account/password", data=change(password))

    assert r.status_code == 400
    assert "Choose a new password, not the temporary one." in r.text
    await db.refresh(user)
    assert user.must_change_password is True
    assert verify_password(user.password_hash, password)

    r = await newcomer.post("/account/password", data=change("a-password-of-my-own"))

    assert r.status_code == 303
    assert r.headers["location"] == "/account"
    await db.refresh(user)
    assert user.must_change_password is False
    assert verify_password(user.password_hash, "a-password-of-my-own")


async def test_created_account_can_be_an_admin(admin_client, db):
    r = await admin_client.post(
        "/admin/users", data={"email": "boss@example.com", "is_admin": "true"}
    )

    assert r.status_code == 200
    assert await db.scalar(select(User.is_admin).where(User.email == "boss@example.com")) is True


@pytest.mark.parametrize(
    ("email", "status_code", "message"),
    [
        ("Admin@Example.com", 409, "An account with this email already exists."),
        ("not-an-email", 422, "Enter a valid email address."),
    ],
)
async def test_creating_an_account_reports_bad_emails(
    admin_client, db, email, status_code, message
):
    r = await admin_client.post("/admin/users", data={"email": email})

    assert r.status_code == status_code
    assert message in r.text
    assert f'value="{email}"' in r.text  # the form keeps what was typed
    assert TEMP_PASSWORD_ON_PAGE.search(r.text) is None
    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def test_delete_user_removes_owned_boards_and_sessions(
    admin_client, client_factory, db, admin, admin_events
):
    dave = await create_user(db, "dave@example.com")
    owned = await create_board(db, dave, "Dave's list")
    await create_item(db, await create_category(db, owned, "Dairy"), "Milk")
    shared = await create_board(db, admin, "Family", {dave: BoardRole.EDITOR})
    device = await signed_in(client_factory, "dave@example.com")
    sessions = await session_ids(db, dave)

    r = await admin_client.delete(f"/admin/users/{dave.id}", headers=HX)

    assert r.status_code == 200
    assert r.text == ""  # htmx swaps the row out
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "info", "message": "Account deleted."}
    }
    assert await db.get(User, dave.id, populate_existing=True) is None
    assert await db.scalar(select(func.count()).select_from(UserSession)) == 1  # the admin's
    assert list(await db.scalars(select(Board.id))) == [shared.id]
    assert await db.scalar(select(func.count()).select_from(Category)) == 0
    assert await db.scalar(select(func.count()).select_from(Item)) == 0
    assert list(await db.scalars(select(BoardMember.user_id))) == [admin.id]
    assert admin_events == [
        BoardEvent(EventType.SESSIONS_REVOKED, user_id=dave.id, session_ids=tuple(sessions)),
        BoardEvent(EventType.BOARD_DELETED, board_id=owned.id),
    ]
    r = await device.get("/boards")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


async def test_admin_cannot_delete_self_or_last_admin(admin_client, db, admin):
    admin_id = admin.id
    r = await admin_client.delete(f"/admin/users/{admin_id}", headers=HX)
    assert r.status_code == 409
    assert toast_message(r) == CANNOT_DELETE_SELF

    r = await admin_client.post(
        f"/admin/users/{admin_id}/status", data={"status": "disabled"}, headers=HX
    )
    assert r.status_code == 409
    assert toast_message(r) == LAST_ADMIN
    assert (await fresh(db, admin)).status == UserStatus.ACTIVE
    assert await session_ids(db, admin) != []

    # A disabled admin does not count: the only active one cannot go, whoever asks.
    former = await create_user(db, "former@example.com", is_admin=True, status=UserStatus.DISABLED)
    with pytest.raises(Conflict, match=LAST_ADMIN):
        await delete_user(db, former, admin_id)
    await db.rollback()
    assert await db.get(User, admin_id, populate_existing=True) is not None

    # With another active admin, the other one can be disabled, and deleted.
    other = await create_user(db, "other-admin@example.com", is_admin=True)
    other_id = other.id
    r = await admin_client.post(f"/admin/users/{other_id}/status", data={"status": "disabled"})
    assert r.status_code == 303
    r = await admin_client.delete(f"/admin/users/{other_id}", headers=HX)
    assert r.status_code == 200
    assert await db.get(User, other_id, populate_existing=True) is None


async def test_two_admins_removing_each_other_leaves_one(client_factory, db, monkeypatch):
    first = await create_user(db, "first@example.com", is_admin=True)
    second = await create_user(db, "second@example.com", is_admin=True)
    first_client = await signed_in(client_factory, first.email)
    second_client = await signed_in(client_factory, second.email)
    # Both requests get as far as loading the other admin before either goes on.
    both_loaded = asyncio.Barrier(2)
    get_user = service.get_user

    async def get_user_at_once(db, user_id):
        user = await get_user(db, user_id)
        await asyncio.wait_for(both_loaded.wait(), 5)
        return user

    monkeypatch.setattr(service, "get_user", get_user_at_once)

    responses = await asyncio.gather(
        first_client.delete(f"/admin/users/{second.id}", headers=HX),
        second_client.delete(f"/admin/users/{first.id}", headers=HX),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert [toast_message(r) for r in responses if r.status_code == 409] == [LAST_ADMIN]
    assert await db.scalar(select(func.count()).select_from(User).where(User.is_admin)) == 1


async def test_send_reset_emails_user(admin_client, client, db, mail_outbox):
    dave = await create_user(db, "dave@example.com")

    r = await admin_client.post(f"/admin/users/{dave.id}/send-reset", headers=HX)

    assert r.status_code == 200
    assert listed_user_ids(r.text) == [dave.id]  # the row, swapped in place
    assert toast_message(r) == "Password reset link sent to dave@example.com."
    assert [mail.to for mail in mail_outbox] == ["dave@example.com"]
    token = RESET_LINK.search(mail_outbox[-1].body)[1]
    assert (await client.get(f"/password/reset/{token}")).status_code == 200


async def test_send_reset_refuses_a_disabled_account(admin_client, db, mail_outbox):
    dave = await create_user(db, "dave@example.com", status=UserStatus.DISABLED)

    r = await admin_client.post(f"/admin/users/{dave.id}/send-reset", headers=HX)

    assert r.status_code == 409
    assert toast_message(r) == "This account is disabled."
    assert mail_outbox == []


async def test_disable_user_kills_sessions(admin_client, client_factory, db, admin_events):
    dave = await create_user(db, "dave@example.com")
    device = await signed_in(client_factory, "dave@example.com")
    sessions = await session_ids(db, dave)

    r = await admin_client.post(f"/admin/users/{dave.id}/status", data={"status": "disabled"})

    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users"
    assert FLASH_COOKIE in r.cookies
    assert (await fresh(db, dave)).status == UserStatus.DISABLED
    assert await session_ids(db, dave) == []
    assert admin_events == [
        BoardEvent(EventType.SESSIONS_REVOKED, user_id=dave.id, session_ids=tuple(sessions))
    ]
    r = await device.get("/boards")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")

    # Enabled again, the account signs in as before.
    r = await admin_client.post(
        f"/admin/users/{dave.id}/status", data={"status": "active"}, headers=HX
    )
    assert r.status_code == 200
    assert toast_message(r) == "Account enabled."
    assert (await fresh(db, dave)).status == UserStatus.ACTIVE
    await signed_in(client_factory, "dave@example.com")


async def test_disabled_account_cannot_sign_in(admin_client, client_factory, db):
    dave = await create_user(db, "dave@example.com")
    await admin_client.post(f"/admin/users/{dave.id}/status", data={"status": "disabled"})

    device = await client_factory()
    await device.get("/login")
    r = await device.post(
        "/login",
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": device.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 400
    assert "This account is disabled." in r.text


async def test_reset_2fa_turns_it_off_and_ends_pending_sign_ins(
    admin_client, client_factory, db, settings, admin_events
):
    dave = await create_user(db, "dave@example.com")
    secret = await begin_setup(db, dave, settings)
    await confirm_setup(db, dave, pyotp.TOTP(secret).now(), settings)
    pending = await client_factory()
    await login(pending, "dave@example.com")  # the password step only: waiting for a code
    pending_ids = await session_ids(db, dave)
    assert len(pending_ids) == 1

    r = await admin_client.post(f"/admin/users/{dave.id}/reset-2fa", headers=HX)

    assert r.status_code == 200
    assert listed_user_ids(r.text) == [dave.id]
    assert toast_message(r) == "Two-factor authentication is off for this account."
    dave = await fresh(db, dave)
    assert (dave.totp_enabled, dave.totp_secret_enc, dave.totp_last_step) == (False, None, None)
    assert await db.scalar(select(func.count()).select_from(RecoveryCode)) == 0
    assert await session_ids(db, dave) == []
    assert admin_events == [
        BoardEvent(EventType.SESSIONS_REVOKED, user_id=dave.id, session_ids=tuple(pending_ids))
    ]
    # Signing in with the password alone now goes straight in, and 2FA can be set up again.
    device = await client_factory()
    await device.get("/login")
    r = await device.post(
        "/login",
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": device.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # not the code form
    assert (await device.get("/")).headers["location"] == "/boards"
    assert (await device.get("/account/2fa/setup")).status_code == 200


async def test_request_racing_the_deletion_of_its_user_is_signed_out(
    client_factory, db, admin, monkeypatch
):
    dave = await create_user(db, "dave@example.com")
    device = await signed_in(client_factory, "dave@example.com")
    load_session = deps.load_session

    async def load_session_then_delete_user(session_db, raw_token):
        session = await load_session(session_db, raw_token)
        await delete_user(db, admin, dave.id)  # an admin, just then
        return session

    monkeypatch.setattr(deps, "load_session", load_session_then_delete_user)

    r = await device.get("/boards")  # not a 500

    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


async def test_admin_cannot_open_users_board(admin_client, db):
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave, "Private")
    await create_category(db, board, "Dairy")

    for path in (
        f"/boards/{board.id}",
        f"/boards/{board.id}/content",
        f"/boards/{board.id}/members",
    ):
        r = await admin_client.get(path)
        assert r.status_code == 404, path
    r = await admin_client.post(f"/boards/{board.id}/categories", data={"name": "Mine now"})
    assert r.status_code == 404
    assert "Private" not in (await admin_client.get("/boards")).text
    assert await db.scalar(select(func.count()).select_from(Category)) == 1


async def test_actions_on_an_unknown_user_are_404(admin_client):
    for method, path, data in ROUTES[3:]:
        r = await admin_client.request(method, path.format(user=999999), data=data, headers=HX)
        assert r.status_code == 404, path
        assert toast_message(r) == "User not found."


def _open_socket(server, client: httpx.AsyncClient, board_id: int):
    url = server.url.replace("http://", "ws://", 1) + f"/ws/boards/{board_id}"
    cookie = {"Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}"}
    return connect(url, origin=server.url, additional_headers=cookie)


async def _assert_admitted(ws) -> None:
    with pytest.raises(TimeoutError):  # the socket stays open
        await asyncio.wait_for(ws.recv(), 0.5)


async def _assert_closed_with(ws, code: int) -> None:
    with pytest.raises(ConnectionClosed):
        while True:  # a deleted board sends its last message first
            await asyncio.wait_for(ws.recv(), 1.0)
    assert ws.close_code == code


def _live_client(server) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, headers={"Origin": server.url})


async def test_disabling_closes_the_users_open_sockets(live_server, db):
    await create_user(db, "admin@example.com", is_admin=True)
    dave = await create_user(db, "dave@example.com")
    board = await create_board(db, dave)
    async with _live_client(live_server) as admin, _live_client(live_server) as device:
        await login(admin, "admin@example.com")
        await login(device, "dave@example.com")
        async with _open_socket(live_server, device, board.id) as ws:
            await _assert_admitted(ws)

            r = await admin.post(f"/admin/users/{dave.id}/status", data={"status": "disabled"})
            assert r.status_code == 303

            await _assert_closed_with(ws, 4401)


async def test_deleting_closes_the_users_sockets_and_those_on_their_boards(live_server, db):
    await create_user(db, "admin@example.com", is_admin=True)
    dave = await create_user(db, "dave@example.com")
    olga = await create_user(db, "olga@example.com")
    daves_board = await create_board(db, dave, "Dave's", {olga: BoardRole.EDITOR})
    olgas_board = await create_board(db, olga, "Olga's", {dave: BoardRole.EDITOR})
    async with (
        _live_client(live_server) as admin,
        _live_client(live_server) as daves_device,
        _live_client(live_server) as olgas_device,
    ):
        await login(admin, "admin@example.com")
        await login(daves_device, "dave@example.com")
        await login(olgas_device, "olga@example.com")
        async with (
            _open_socket(live_server, daves_device, olgas_board.id) as daves_ws,
            _open_socket(live_server, olgas_device, daves_board.id) as olgas_ws,
        ):
            await _assert_admitted(daves_ws)
            await _assert_admitted(olgas_ws)

            r = await admin.delete(f"/admin/users/{dave.id}", headers=HX)
            assert r.status_code == 200

            await _assert_closed_with(daves_ws, 4401)  # signed out
            await _assert_closed_with(olgas_ws, 4404)  # the board is gone
