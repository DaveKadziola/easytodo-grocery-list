"""The board WebSocket on a live server (real Uvicorn, real LISTEN/NOTIFY): who may open it, what
it receives, and when the server closes it."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from factories import create_board, create_category, create_user
from sqlalchemy import select, update
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from easytodo.auth.sessions import SESSION_COOKIE, create_session
from easytodo.models import BoardMember, BoardRole, User, UserSession, UserStatus
from easytodo.security.tokens import hash_token

WAIT_S = 1.0
HX = {"HX-Request": "true"}
SAME_ORIGIN = object()  # board_socket's default: the server's own origin


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


async def close_code(ws: ClientConnection) -> int:
    """The code the server closed ``ws`` with; nothing may arrive before the close."""
    try:
        message = await asyncio.wait_for(ws.recv(), WAIT_S)
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
async def test_ws_rejects_foreign_origin_with_4403(
    live_server, groceries, sign_in, subscribed, origin
):
    board, _ = groceries
    dave = await sign_in(live_server, "dave@example.com")

    async with board_socket(live_server, board.id, token=token_of(dave), origin=origin) as ws:
        assert await close_code(ws) == 4403
    assert subscribed == []


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
