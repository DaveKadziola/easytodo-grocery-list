"""The board and user WebSockets on a live server (real Uvicorn, real LISTEN/NOTIFY): who may open
them, what they receive, and when the server closes them."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from factories import create_board, create_category, create_notification, create_user
from live_server import LiveServer, emptying_tables, serving
from sqlalchemy import delete, select, text, update
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from easytodo.auth.sessions import SESSION_COOKIE, create_session
from easytodo.models import BoardMember, BoardRole, User, UserSession, UserStatus
from easytodo.realtime import hub as hub_module
from easytodo.realtime import routes
from easytodo.realtime.listener import LISTENER_APPLICATION_NAME
from easytodo.security.tokens import hash_token
from realtime.conftest import spy_subscribe

WAIT_S = 1.0
HX = {"HX-Request": "true"}
SAME_ORIGIN = object()  # board_socket's default: the server's own origin
EXTRA_ORIGIN = "http://192.168.122.50:8000"  # a second address the app is opened at


@asynccontextmanager
async def board_socket(
    server,
    board_id: int,
    *,
    token: str | None = None,
    client_id: str | None = None,
    origin: object = SAME_ORIGIN,
) -> AsyncIterator[ClientConnection]:
    """A WebSocket on ``/ws/boards/{board_id}`` with the session cookie ``token``."""
    url = server.url.replace("http://", "ws://", 1) + f"/ws/boards/{board_id}"
    if client_id is not None:
        url += f"?client_id={client_id}"
    headers = {"Cookie": f"{SESSION_COOKIE}={token}"} if token else {}
    async with connect(
        url,
        origin=server.url if origin is SAME_ORIGIN else origin,
        additional_headers=headers,
    ) as ws:
        yield ws


@asynccontextmanager
async def user_socket(
    server,
    *,
    token: str | None = None,
    client_id: str | None = None,
    origin: object = SAME_ORIGIN,
) -> AsyncIterator[ClientConnection]:
    """A WebSocket on ``/ws/user`` with the session cookie ``token``."""
    url = server.url.replace("http://", "ws://", 1) + "/ws/user"
    if client_id is not None:
        url += f"?client_id={client_id}"
    headers = {"Cookie": f"{SESSION_COOKIE}={token}"} if token else {}
    async with connect(
        url,
        origin=server.url if origin is SAME_ORIGIN else origin,
        additional_headers=headers,
    ) as ws:
        yield ws


def token_of(device: httpx.AsyncClient) -> str:
    return device.cookies[SESSION_COOKIE]


async def session_id_of(db, device: httpx.AsyncClient) -> int:
    return await db.scalar(
        select(UserSession.id).where(UserSession.token_hash == hash_token(token_of(device)))
    )


async def next_message(ws: ClientConnection) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), WAIT_S))


async def assert_no_message(ws: ClientConnection, within: float = 0.5) -> None:
    with pytest.raises(TimeoutError):
        message = await asyncio.wait_for(ws.recv(), within)
        pytest.fail(f"received {message}")


async def close_code(ws: ClientConnection, within: float = WAIT_S) -> int:
    """The code the server closed ``ws`` with; nothing may arrive before the close."""
    try:
        message = await asyncio.wait_for(ws.recv(), within)
    except ConnectionClosed:
        return ws.close_code
    pytest.fail(f"received {message} instead of the close")


@pytest.fixture
async def dave(db) -> User:
    return await create_user(db, "dave@example.com")


@pytest.fixture
async def groceries(db, dave):
    """Dave's board "Groceries" with the category "Dairy"; returns (board, dairy)."""
    board = await create_board(db, dave, "Groceries")
    return board, await create_category(db, board, "Dairy")


async def add_item(device: httpx.AsyncClient, category_id: int, client_id: str) -> None:
    r = await device.post(
        f"/categories/{category_id}/items",
        data={"name": "Milk"},
        headers=HX | {"X-Client-Id": client_id},
    )
    assert r.status_code == 200, r.text


async def test_change_on_device_a_reaches_device_b_within_1s(
    live_server, groceries, sign_in, subscribed
):
    board, dairy = groceries
    a = await sign_in(live_server, "dave@example.com")
    b = await sign_in(live_server, "dave@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(a), client_id="A") as ws_a,
        board_socket(live_server, board.id, token=token_of(b), client_id="B") as ws_b,
    ):
        started = time.monotonic()
        await add_item(a, dairy.id, client_id="A")

        assert await next_message(ws_b) == {
            "type": "category.changed",
            "board_id": board.id,
            "category_ids": [dairy.id],
        }
        assert time.monotonic() - started < 1
        await assert_no_message(ws_a)  # its own change is already on screen
    assert [s.client_id for s in subscribed] == ["A", "B"]


async def test_ws_rejects_anonymous_with_4401(live_server, groceries, subscribed):
    board, _ = groceries
    for token in (None, "not-a-session-token"):
        async with board_socket(live_server, board.id, token=token) as ws:
            assert await close_code(ws) == 4401
    assert subscribed == []


@pytest.mark.parametrize("board_id", ["someone_elses", "missing", 2**63])
async def test_ws_rejects_non_member_with_4404(
    live_server, db, groceries, sign_in, subscribed, board_id
):
    board, _ = groceries
    await create_user(db, "eve@example.com")
    eve = await sign_in(live_server, "eve@example.com")
    board_id = {"someone_elses": board.id, "missing": board.id + 1000}.get(board_id, board_id)

    async with board_socket(live_server, board_id, token=token_of(eve)) as ws:
        assert await close_code(ws) == 4404
    assert subscribed == []


@pytest.mark.parametrize("origin", ["http://evil.example", None])
async def test_ws_rejects_foreign_origin_with_4400(
    live_server, groceries, sign_in, subscribed, origin
):
    """Its own code (not 4403, "removed from the board"): the page is at another address than
    EASYTODO_PUBLIC_URL (or EASYTODO_EXTRA_ORIGINS), and says so instead of leaving the board."""
    board, _ = groceries
    dave = await sign_in(live_server, "dave@example.com")

    async with board_socket(live_server, board.id, token=token_of(dave), origin=origin) as ws:
        assert await close_code(ws) == 4400
    assert subscribed == []


@pytest.fixture
def live_server_with_extra_origin(settings) -> Iterator[LiveServer]:
    """A live server whose EASYTODO_EXTRA_ORIGINS is ``EXTRA_ORIGIN``."""
    extra = settings.model_copy(update={"extra_origins": [EXTRA_ORIGIN]})
    with emptying_tables(extra), serving(extra) as server:
        yield server


async def test_ws_admits_an_extra_origin_and_still_rejects_others(
    live_server_with_extra_origin, db, dave, groceries, sign_in, monkeypatch
):
    """Both sockets (#159) take the extra address the app is opened at, and no other."""
    server = live_server_with_extra_origin
    subscribed = spy_subscribe(server, monkeypatch)
    board, dairy = groceries
    await create_notification(db, dave)
    device = await sign_in(server, "dave@example.com")

    async with board_socket(server, board.id, token=token_of(device), origin=EXTRA_ORIGIN) as ws:
        await add_item(device, dairy.id, client_id="other-tab")
        assert (await next_message(ws))["type"] == "category.changed"  # admitted: it is live
    async with user_socket(
        server, token=token_of(device), client_id="this-tab", origin=EXTRA_ORIGIN
    ) as ws:
        r = await device.post("/notifications/read", headers=HX | {"X-Client-Id": "other-tab"})
        assert r.status_code == 200, r.text
        assert await next_message(ws) == {"type": "notifications.changed"}  # live as well
    other = "http://192.168.1.100:8000"
    async with board_socket(server, board.id, token=token_of(device), origin=other) as ws:
        assert await close_code(ws) == 4400
    async with user_socket(server, token=token_of(device), origin=other) as ws:
        assert await close_code(ws) == 4400
    assert [s.board_id for s in subscribed] == [board.id, None]


async def test_removal_between_the_admission_check_and_the_subscription_still_closes(
    live_server, db, dave, groceries, sign_in, monkeypatch
):
    """A member removed after the socket's membership check, but before it is subscribed (so
    the removal's event reached nobody), is still refused: the check runs again once the socket
    is subscribed."""
    board, _ = groceries
    eve = await create_user(db, "eve@example.com")
    db.add(BoardMember(board_id=board.id, user_id=eve.id, role=BoardRole.EDITOR))
    await db.commit()
    member = await sign_in(live_server, "eve@example.com")
    admit = routes._admit
    calls = []

    async def admit_then_remove(websocket, board_id):
        admitted = await admit(websocket, board_id)
        calls.append(admitted)
        if len(calls) == 1:  # removed right after the first check, without an event
            async with websocket.app.state.sessionmaker() as other:
                await other.execute(
                    delete(BoardMember).where(
                        BoardMember.board_id == board_id, BoardMember.user_id == eve.id
                    )
                )
                await other.commit()
        return admitted

    monkeypatch.setattr(routes, "_admit", admit_then_remove)

    async with board_socket(live_server, board.id, token=token_of(member)) as ws:
        assert await close_code(ws) == 4404
    assert live_server.app.state.hub.subscribers() == []


async def test_ws_rejects_must_change_password_user_with_4401(
    live_server, db, dave, groceries, sign_in, subscribed
):
    board, _ = groceries
    device = await sign_in(live_server, "dave@example.com")
    await db.execute(update(User).where(User.id == dave.id).values(must_change_password=True))
    await db.commit()

    async with board_socket(live_server, board.id, token=token_of(device)) as ws:
        assert await close_code(ws) == 4401
    assert subscribed == []


@pytest.mark.parametrize("state", ["mfa_pending", "expired", "disabled_user"])
async def test_ws_rejects_a_session_that_is_not_fully_signed_in_with_4401(
    live_server, db, dave, groceries, subscribed, state
):
    board, _ = groceries
    token, session = await create_session(
        db,
        dave,
        mfa_pending=state == "mfa_pending",
        user_agent=None,
        ip=None,
        settings=live_server.settings,
    )
    if state == "expired":
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if state == "disabled_user":
        dave.status = UserStatus.DISABLED
    await db.commit()

    async with board_socket(live_server, board.id, token=token) as ws:
        assert await close_code(ws) == 4401
    assert subscribed == []


async def test_removed_member_socket_is_closed(live_server, db, dave, groceries, sign_in):
    board, _ = groceries
    eve = await create_user(db, "eve@example.com")
    db.add(BoardMember(board_id=board.id, user_id=eve.id, role=BoardRole.EDITOR))
    await db.commit()
    owner = await sign_in(live_server, "dave@example.com")
    member = await sign_in(live_server, "eve@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(member)) as ws_member,
        board_socket(live_server, board.id, token=token_of(owner), client_id="phone") as ws_owner,
    ):
        r = await owner.delete(
            f"/boards/{board.id}/members/{eve.id}", headers=HX | {"X-Client-Id": "laptop"}
        )
        assert r.status_code == 200, r.text

        assert await next_message(ws_member) == {
            "type": "member.removed",
            "board_id": board.id,
            "category_ids": [],
        }
        assert await close_code(ws_member) == 4403
        assert await next_message(ws_owner) == {
            "type": "board.updated",
            "board_id": board.id,
            "category_ids": [],
        }


async def test_archiving_closes_the_board_sockets_and_refuses_new_ones(
    live_server, db, dave, groceries, sign_in
):
    board, dairy = groceries
    eve = await create_user(db, "eve@example.com")
    db.add(BoardMember(board_id=board.id, user_id=eve.id, role=BoardRole.EDITOR))
    await db.commit()
    owner = await sign_in(live_server, "dave@example.com")
    member = await sign_in(live_server, "eve@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(member)) as ws_member,
        board_socket(live_server, board.id, token=token_of(owner), client_id="phone") as ws_owner,
    ):
        r = await owner.post(f"/boards/{board.id}/archive", headers=HX | {"X-Client-Id": "phone"})
        assert r.status_code == 200, r.text

        for ws in (ws_member, ws_owner):  # the archiving tab too: it leaves the board
            assert await next_message(ws) == {
                "type": "board.deleted",
                "board_id": board.id,
                "category_ids": [],
            }
            assert await close_code(ws) == 4404
    for device in (owner, member):  # a page still open on the board, reconnecting
        async with board_socket(live_server, board.id, token=token_of(device)) as ws:
            assert await close_code(ws) == 4404

    r = await owner.post(f"/boards/{board.id}/restore", headers=HX)
    assert r.status_code == 200, r.text
    async with board_socket(live_server, board.id, token=token_of(member), client_id="M") as ws:
        await add_item(owner, dairy.id, client_id="O")
        assert (await next_message(ws))["type"] == "category.changed"


async def test_logout_closes_that_sessions_socket(live_server, groceries, sign_in):
    board, dairy = groceries
    a = await sign_in(live_server, "dave@example.com")
    b = await sign_in(live_server, "dave@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(a), client_id="A") as ws_a,
        board_socket(live_server, board.id, token=token_of(b), client_id="B") as ws_b,
    ):
        r = await a.post("/logout")
        assert r.status_code == 303

        assert await close_code(ws_a) == 4401
        await add_item(b, dairy.id, client_id="B2")  # another tab of device B
        assert (await next_message(ws_b))["type"] == "category.changed"  # B is still live


async def test_password_change_closes_the_other_sessions_sockets(live_server, groceries, sign_in):
    board, dairy = groceries
    a = await sign_in(live_server, "dave@example.com")
    b = await sign_in(live_server, "dave@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(a), client_id="A") as ws_a,
        board_socket(live_server, board.id, token=token_of(b), client_id="B") as ws_b,
    ):
        r = await a.post(
            "/account/password",
            data={
                "current_password": "correct-horse-battery-staple",
                "new_password": "a-brand-new-passphrase",
                "new_password_confirm": "a-brand-new-passphrase",
            },
        )
        assert r.status_code == 303, r.text

        assert await close_code(ws_b) == 4401
        await add_item(a, dairy.id, client_id="A2")
        assert (await next_message(ws_a))["type"] == "category.changed"  # A stays signed in


async def test_revoking_one_session_closes_only_its_socket(live_server, db, groceries, sign_in):
    board, dairy = groceries
    a, b, c = [await sign_in(live_server, "dave@example.com") for _ in range(3)]

    async with (
        board_socket(live_server, board.id, token=token_of(b), client_id="B") as ws_b,
        board_socket(live_server, board.id, token=token_of(c), client_id="C") as ws_c,
    ):
        r = await a.post(f"/account/sessions/{await session_id_of(db, b)}/revoke")
        assert r.status_code == 303

        assert await close_code(ws_b) == 4401
        await add_item(a, dairy.id, client_id="A")
        assert (await next_message(ws_c))["type"] == "category.changed"


async def test_events_cross_workers(live_server_two_workers, groceries, sign_in):
    first, second = live_server_two_workers
    board, dairy = groceries
    device = await sign_in(first, "dave@example.com")

    async with board_socket(second, board.id, token=token_of(device), client_id="B") as ws:
        await add_item(device, dairy.id, client_id="A")  # handled by the first worker

        assert await next_message(ws) == {
            "type": "category.changed",
            "board_id": board.id,
            "category_ids": [dairy.id],
        }


async def wait_for_sockets(server, count: int) -> None:
    """Wait (up to 1 s) until the server's hub holds ``count`` sockets: the handler of a socket
    the page closed unsubscribes it a moment later."""
    hub = server.app.state.hub
    for _ in range(100):
        if len(hub.subscribers()) == count:
            return
        await asyncio.sleep(0.01)
    assert len(hub.subscribers()) == count


async def test_user_socket_gets_notifications_changed_within_1s(
    live_server, db, dave, groceries, sign_in, subscribed
):
    _, dairy = groceries
    await create_notification(db, dave)
    a = await sign_in(live_server, "dave@example.com")
    b = await sign_in(live_server, "dave@example.com")

    async with (
        user_socket(live_server, token=token_of(a), client_id="A") as ws_a,
        user_socket(live_server, token=token_of(b), client_id="B") as ws_b,
    ):
        await add_item(a, dairy.id, client_id="A")
        await assert_no_message(ws_b)  # board events are for the board pages
        started = time.monotonic()
        r = await a.post("/notifications/read", headers=HX | {"X-Client-Id": "A"})
        assert r.status_code == 200, r.text

        assert await next_message(ws_b) == {"type": "notifications.changed"}
        assert time.monotonic() - started < 1
        await assert_no_message(ws_a)  # its own tab shows the read already

        r = await a.post("/logout")
        assert r.status_code == 303
        assert await close_code(ws_a) == 4401
    assert [(s.board_id, s.client_id) for s in subscribed] == [(None, "A"), (None, "B")]


@pytest.mark.parametrize(
    "case,code",
    [("anonymous", 4401), ("foreign_origin", 4400), ("must_change_password", 4401)],
)
async def test_user_socket_refuses(live_server, db, dave, sign_in, subscribed, case, code):
    """As the board socket does: its own code for each reason, and nothing subscribed."""
    device = await sign_in(live_server, "dave@example.com")
    token, origin = token_of(device), SAME_ORIGIN
    if case == "anonymous":
        token = None
    elif case == "foreign_origin":
        origin = "http://evil.example"
    else:
        await db.execute(update(User).where(User.id == dave.id).values(must_change_password=True))
        await db.commit()

    async with user_socket(live_server, token=token, origin=origin) as ws:
        assert await close_code(ws) == code
    assert subscribed == []


async def test_notifications_reach_a_user_socket_on_another_worker(
    live_server_two_workers, db, dave, sign_in
):
    first, second = live_server_two_workers
    await create_notification(db, dave)
    device = await sign_in(first, "dave@example.com")

    async with user_socket(second, token=token_of(device), client_id="B") as ws:
        r = await device.post("/notifications/read", headers=HX | {"X-Client-Id": "A"})
        assert r.status_code == 200, r.text  # handled by the first worker

        assert await next_message(ws) == {"type": "notifications.changed"}


async def test_listener_reconnect_closes_board_and_user_sockets_with_1013(
    live_server, db, dave, groceries, sign_in
):
    """#88: events may have been missed while the listener was away, so every socket goes (and
    its page reconnects, which checks the session and the membership again)."""
    board, _ = groceries
    device = await sign_in(live_server, "dave@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(device)) as board_ws,
        user_socket(live_server, token=token_of(device)) as user_ws,
    ):
        terminated = await db.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                " WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": LISTENER_APPLICATION_NAME},
        )
        await db.commit()
        assert terminated == 1

        assert await close_code(board_ws, within=5) == 1013
        assert await close_code(user_ws, within=5) == 1013


async def test_sockets_close_with_4401_when_the_session_expires(
    live_server, db, dave, groceries, sign_in
):
    """#93: a socket outlives the request that admitted it, so the session's end closes it."""
    board, _ = groceries
    device = await sign_in(live_server, "dave@example.com")
    lifetime = 1.5
    started = time.monotonic()  # before the end is fixed: the handshakes below come after it
    await db.execute(
        update(UserSession)
        .where(UserSession.id == await session_id_of(db, device))
        .values(expires_at=datetime.now(UTC) + timedelta(seconds=lifetime))
    )
    await db.commit()

    async with (
        board_socket(live_server, board.id, token=token_of(device)) as board_ws,
        user_socket(live_server, token=token_of(device)) as user_ws,
    ):
        assert await close_code(board_ws, within=lifetime + 1.5) == 4401
        assert await close_code(user_ws, within=1) == 4401
        assert time.monotonic() - started > lifetime - 0.5  # at the end of the session, not before


async def test_sockets_over_the_per_session_cap_are_refused_with_1013(
    live_server, groceries, sign_in, monkeypatch, subscribed
):
    """#94: one session may hold only so many sockets (board pages and user sockets together), so
    a script cannot exhaust the server's file descriptors with one cookie. Tabs that find the
    budget spent retry later (1013)."""
    board, _ = groceries
    monkeypatch.setattr(hub_module, "MAX_SOCKETS_PER_SESSION", 2)
    device = await sign_in(live_server, "dave@example.com")
    other_device = await sign_in(live_server, "dave@example.com")

    async with (
        board_socket(live_server, board.id, token=token_of(device)),
        user_socket(live_server, token=token_of(device)),
    ):
        async with user_socket(live_server, token=token_of(device)) as refused:
            assert await close_code(refused) == 1013
        async with board_socket(live_server, board.id, token=token_of(device)) as refused:
            assert await close_code(refused) == 1013
        assert len(live_server.app.state.hub.subscribers()) == 2  # the refused ones never joined
        async with user_socket(live_server, token=token_of(other_device)) as elsewhere:
            await assert_no_message(elsewhere)  # another session has its own budget
    await wait_for_sockets(live_server, 0)

    async with user_socket(live_server, token=token_of(device)) as again:
        await assert_no_message(again)  # the places came back
    assert len(subscribed) == 6  # every socket asked, the refused ones too
