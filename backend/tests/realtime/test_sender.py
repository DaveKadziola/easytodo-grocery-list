"""A socket's sending side (realtime.routes) against a stand-in WebSocket: what is queued when the
hub closes the socket still goes out first, a page that went away is not closed, and a socket whose
session expires is closed at that moment."""

import asyncio
from datetime import UTC, datetime, timedelta

from easytodo.realtime.hub import CloseCode, Subscriber
from easytodo.realtime.routes import _serve


class StandInSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closes: list[int] = []  # the codes it was closed with
        self.gone = asyncio.Event()  # set: the page went away

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int) -> None:
        self.closes.append(code)

    async def receive(self) -> dict:
        await self.gone.wait()
        return {"type": "websocket.disconnect"}


async def test_everything_queued_is_sent_before_the_hubs_close():
    sub = Subscriber(board_id=1, user_id=1, session_id=1, client_id=None)
    for n in range(3):
        sub.queue.put_nowait(f"message {n}")
    sub.close(CloseCode.FORBIDDEN)
    socket = StandInSocket()

    await asyncio.wait_for(_serve(socket, sub), timeout=1)

    assert socket.sent == ["message 0", "message 1", "message 2"]
    assert socket.closes == [CloseCode.FORBIDDEN]


async def test_a_page_that_went_away_is_not_closed_again():
    sub = Subscriber(board_id=1, user_id=1, session_id=1, client_id=None)
    socket = StandInSocket()
    serving = asyncio.create_task(_serve(socket, sub))
    sub.queue.put_nowait("message")
    await asyncio.sleep(0.05)

    socket.gone.set()
    await asyncio.wait_for(serving, timeout=1)

    assert socket.sent == ["message"]
    assert socket.closes == []


def expiring(seconds: float) -> Subscriber:
    return Subscriber(
        board_id=None,
        user_id=1,
        session_id=1,
        client_id=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


async def test_socket_closes_with_4401_when_its_session_expires():
    """The session's end (#93) closes the socket then and there: the page signs in again."""
    sub = expiring(0.3)
    socket = StandInSocket()
    serving = asyncio.create_task(_serve(socket, sub))
    sub.queue.put_nowait("before")
    await asyncio.sleep(0.1)
    assert socket.sent == ["before"] and socket.closes == []  # signed in until then

    await asyncio.wait_for(serving, timeout=2)

    assert socket.sent == ["before"]
    assert socket.closes == [CloseCode.NOT_AUTHENTICATED]


async def test_an_expired_session_gets_no_queue_before_the_4401():
    sub = expiring(-1)  # expired while the socket was being admitted
    for n in range(3):
        sub.queue.put_nowait(f"message {n}")
    socket = StandInSocket()

    await asyncio.wait_for(_serve(socket, sub), timeout=1)

    assert socket.sent == []
    assert socket.closes == [CloseCode.NOT_AUTHENTICATED]


async def test_a_socket_without_an_end_of_session_goes_on():
    sub = Subscriber(board_id=1, user_id=1, session_id=1, client_id=None)
    socket = StandInSocket()
    serving = asyncio.create_task(_serve(socket, sub))

    await asyncio.sleep(0.1)

    assert not serving.done()
    socket.gone.set()
    await asyncio.wait_for(serving, timeout=1)
    assert socket.closes == []
