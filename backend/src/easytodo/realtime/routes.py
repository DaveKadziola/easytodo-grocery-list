"""``WS /ws/boards/{board_id}?client_id=…``: the board page's live updates.

The server only sends: ``{"type", "board_id", "category_ids"}`` messages from the hub, plus
``"item_ids"`` when there are some (the page fetches the HTML it needs itself), then possibly a
close code. The page sends nothing.

verify_csrf skips WebSockets, so this route is their only guard: before anything is subscribed
or sent it checks the ``Origin`` (4400), the session (4401) and the membership (4404), and once
the socket is subscribed it checks the session and membership again, so a revocation or removal
committed in between (whose event went out before the socket could get it) still refuses it. A
refused socket is accepted and closed at once with that code, because a socket closed before
the handshake reaches the browser as a bare HTTP 403, without the code the page acts on.
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from easytodo.auth.deps import websocket_user
from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.boards.permissions import Action, require_board
from easytodo.errors import NotFound
from easytodo.middleware import CLIENT_ID_MAX_LENGTH
from easytodo.realtime.hub import CloseCode, Subscriber

router = APIRouter()


@router.websocket("/ws/boards/{board_id}")
async def board_socket(websocket: WebSocket, board_id: int, client_id: str | None = None) -> None:
    admitted = await _admit(websocket, board_id)
    if isinstance(admitted, CloseCode):
        await websocket.accept()
        await websocket.close(admitted)
        return
    user_id, session_id = admitted
    if client_id is not None and not 0 < len(client_id) <= CLIENT_ID_MAX_LENGTH:
        client_id = None  # as ClientIdMiddleware ignores such an X-Client-Id
    sub = Subscriber(board_id, user_id, session_id, client_id)
    hub = websocket.app.state.hub
    hub.subscribe(sub)  # before the handshake completes: no event slips between the two
    try:
        # From now on every revocation or removal reaches ``sub``; one from before is in the
        # database by now.
        again = await _admit(websocket, board_id)
        await websocket.accept()
        if again != admitted:
            await websocket.close(again if isinstance(again, CloseCode) else CloseCode.NOT_FOUND)
            return
        await _serve(websocket, sub)
    except WebSocketDisconnect:
        pass  # the page went away mid-send
    finally:
        hub.unsubscribe(sub)


async def _admit(websocket: WebSocket, board_id: int) -> tuple[int, int] | CloseCode:
    """The user and session ids to subscribe, or the code to refuse the socket with."""
    if websocket.headers.get("origin") not in websocket.app.state.settings.allowed_origins:
        return CloseCode.WRONG_ORIGIN
    # A session of its own, closed again before the socket's (long) life: no pooled connection
    # is held per open board page.
    async with websocket.app.state.sessionmaker() as db:
        signed_in = await websocket_user(db, websocket.cookies.get(SESSION_COOKIE))
        if signed_in is None:
            return CloseCode.NOT_AUTHENTICATED
        user, session = signed_in
        try:
            await require_board(db, board_id, user, Action.VIEW)
        except NotFound:
            return CloseCode.NOT_FOUND
        return user.id, session.id


async def _serve(websocket: WebSocket, sub: Subscriber) -> None:
    """Send ``sub``'s messages until the page goes away or the hub closes ``sub``; then send
    what is still queued and close the socket with the hub's code."""
    gone = asyncio.create_task(_until_disconnected(websocket))
    closing = asyncio.create_task(sub.closing.wait())
    message: asyncio.Task[str] | None = None
    try:
        while not (gone.done() or closing.done()):
            message = asyncio.create_task(sub.queue.get())
            await asyncio.wait({message, gone, closing}, return_when=asyncio.FIRST_COMPLETED)
            if message.done():
                await websocket.send_text(message.result())
            else:
                message.cancel()  # right away, so it cannot take a message off the queue
        if gone.done():
            return
        while not sub.queue.empty():
            await websocket.send_text(sub.queue.get_nowait())
        await websocket.close(sub.close_code)
    finally:
        tasks = [task for task in (gone, closing, message) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _until_disconnected(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass  # the page sends nothing; whatever it does send is ignored
