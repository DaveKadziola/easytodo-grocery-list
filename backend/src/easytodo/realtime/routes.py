"""``WS /ws/boards/{board_id}?client_id=…``: the board page's live updates, and
``WS /ws/user?client_id=…``: the notifications of every signed-in page.

The server only sends: ``{"type", "board_id", "category_ids"}`` messages from the hub, plus
``"item_ids"`` when there are some (the page fetches the HTML it needs itself), or just
``{"type": "notifications.changed"}`` on the user socket, then possibly a close code. The page
sends nothing.

verify_csrf skips WebSockets, so these routes are their only guard: before anything is subscribed
or sent they check the ``Origin`` (4400), the session (4401) and, for a board, the membership
(4404), and once the socket is subscribed they check the session and membership again, so a
revocation or removal committed in between (whose event went out before the socket could get it)
still refuses it. A refused socket is accepted and closed at once with that code, because a socket
closed before the handshake reaches the browser as a bare HTTP 403, without the code the page acts
on. A session may hold only so many sockets (1013 above that: the tab tries again later), and a
socket is closed with 4401 when its session expires.
"""

import asyncio
from datetime import UTC, datetime

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
    await _connect(websocket, board_id, client_id)


@router.websocket("/ws/user")
async def user_socket(websocket: WebSocket, client_id: str | None = None) -> None:
    await _connect(websocket, None, client_id)


async def _connect(websocket: WebSocket, board_id: int | None, client_id: str | None) -> None:
    """Admit, subscribe and serve the socket of a board page (``board_id``) or of a user
    (``None``)."""
    admitted = await _admit(websocket, board_id)
    if isinstance(admitted, CloseCode):
        await websocket.accept()
        await websocket.close(admitted)
        return
    user_id, session_id, expires_at = admitted
    if client_id is not None and not 0 < len(client_id) <= CLIENT_ID_MAX_LENGTH:
        client_id = None  # as ClientIdMiddleware ignores such an X-Client-Id
    sub = Subscriber(board_id, user_id, session_id, client_id, expires_at=expires_at)
    hub = websocket.app.state.hub
    if not hub.subscribe(sub):  # before the handshake completes: no event slips between the two
        await websocket.accept()
        await websocket.close(CloseCode.TRY_AGAIN_LATER)  # the session holds all it may
        return
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


async def _admit(
    websocket: WebSocket, board_id: int | None
) -> tuple[int, int, datetime] | CloseCode:
    """The user id, the session id and the moment the session expires, to subscribe with, or the
    code to refuse the socket with. A ``board_id`` must be one of a board the user is a member
    of; ``None`` is for the user's own socket."""
    if websocket.headers.get("origin") not in websocket.app.state.settings.allowed_origins:
        return CloseCode.WRONG_ORIGIN
    # A session of its own, closed again before the socket's (long) life: no pooled connection
    # is held per open board page.
    async with websocket.app.state.sessionmaker() as db:
        signed_in = await websocket_user(db, websocket.cookies.get(SESSION_COOKIE))
        if signed_in is None:
            return CloseCode.NOT_AUTHENTICATED
        user, session = signed_in
        if board_id is not None:
            try:
                await require_board(db, board_id, user, Action.VIEW)
            except NotFound:
                return CloseCode.NOT_FOUND
        return user.id, session.id, session.expires_at


async def _serve(websocket: WebSocket, sub: Subscriber) -> None:
    """Send ``sub``'s messages until the page goes away, the hub closes ``sub`` or its session
    expires. The hub's close sends what is still queued first; the session's end closes the
    socket with 4401 at once (nothing queued is sent to a page that is signed out)."""
    gone = asyncio.create_task(_until_disconnected(websocket))
    closing = asyncio.create_task(sub.closing.wait())
    expired = asyncio.create_task(_until(sub.expires_at))
    message: asyncio.Task[str] | None = None
    try:
        while not (gone.done() or closing.done() or expired.done()):
            message = asyncio.create_task(sub.queue.get())
            await asyncio.wait(
                {message, gone, closing, expired}, return_when=asyncio.FIRST_COMPLETED
            )
            if expired.done():
                break
            if message.done():
                await websocket.send_text(message.result())
            else:
                message.cancel()  # right away, so it cannot take a message off the queue
        if gone.done():
            return
        if expired.done():
            await websocket.close(CloseCode.NOT_AUTHENTICATED)
            return
        while not sub.queue.empty():
            await websocket.send_text(sub.queue.get_nowait())
        await websocket.close(sub.close_code)
    finally:
        tasks = [task for task in (gone, closing, expired, message) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _until(moment: datetime | None) -> None:
    """Return at ``moment``; never, without one."""
    if moment is None:
        await asyncio.Event().wait()
    else:
        await asyncio.sleep(max(0.0, (moment - datetime.now(UTC)).total_seconds()))


async def _until_disconnected(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass  # the page sends nothing; whatever it does send is ignored
