"""A board socket's sending side (realtime.routes) against a stand-in WebSocket: what is queued
when the hub closes the socket still goes out first, and a page that went away is not closed."""

import asyncio

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
