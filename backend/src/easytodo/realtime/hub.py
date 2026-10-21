"""The board sockets of one worker, and which of them get each event.

Every board page holds a WebSocket (``realtime.routes``) subscribed here as a ``Subscriber``.
``dispatch`` queues an event's message for the sockets that need it, or has a socket closed:
the page then reconnects (and reloads the board), signs in again or leaves the board, by the
close code. The hub keeps only memory state: it never reads the database.
"""

import asyncio
import json
from dataclasses import dataclass, field
from enum import IntEnum

from easytodo.realtime.events import BoardEvent, EventType

QUEUE_SIZE = 100  # messages a socket may fall behind before it is closed


class CloseCode(IntEnum):
    TRY_AGAIN_LATER = 1013  # fell behind: reconnect, which reloads the board
    NOT_AUTHENTICATED = 4401  # signed out, or the session was revoked
    FORBIDDEN = 4403  # no longer a member, or a foreign Origin
    NOT_FOUND = 4404  # the board was deleted, or the user is not a member of it


@dataclass(eq=False)  # each socket is its own subscriber, however alike
class Subscriber:
    board_id: int
    user_id: int
    session_id: int
    client_id: str | None  # the browser tab (X-Client-Id), whose own changes it does not get
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    close_code: int | None = None  # set when the hub closes the socket
    closing: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self, code: CloseCode) -> None:
        """Close the socket with ``code`` once the messages still queued are sent."""
        if self.close_code is None:
            self.close_code = code
            self.closing.set()


def _message(event: BoardEvent, type_: EventType | None = None, *, reload: bool = False) -> str:
    message: dict[str, object] = {
        "type": type_ or event.type,
        "board_id": event.board_id,
        "category_ids": list(event.category_ids),
    }
    if reload:
        message["reload"] = True
    return json.dumps(message)


class Hub:
    def __init__(self) -> None:
        self._boards: dict[int, set[Subscriber]] = {}

    def subscribe(self, sub: Subscriber) -> None:
        self._boards.setdefault(sub.board_id, set()).add(sub)

    def unsubscribe(self, sub: Subscriber) -> None:
        subscribers = self._boards.get(sub.board_id)
        if subscribers is not None:
            subscribers.discard(sub)
            if not subscribers:
                del self._boards[sub.board_id]

    def subscribers(self) -> list[Subscriber]:
        """Every socket subscribed, on any board."""
        return [sub for subscribers in self._boards.values() for sub in subscribers]

    async def dispatch(self, event: BoardEvent) -> None:
        if event.type == EventType.SESSIONS_REVOKED:
            revoked = set(event.session_ids)
            for sub in self.subscribers():
                if sub.session_id in revoked:
                    self._close_now(sub, CloseCode.NOT_AUTHENTICATED)
            return
        if event.board_id is None:
            return
        for sub in list(self._boards.get(event.board_id, ())):
            if event.type == EventType.BOARD_DELETED:
                self._close_after(sub, _message(event), CloseCode.NOT_FOUND)
            elif event.type == EventType.MEMBER_REMOVED and sub.user_id == event.user_id:
                self._close_after(sub, _message(event), CloseCode.FORBIDDEN)
            elif event.origin is not None and sub.client_id == event.origin:
                continue  # the tab that made the change shows it already
            elif event.type == EventType.MEMBER_REMOVED:
                self._send(sub, _message(event, EventType.BOARD_UPDATED))  # the member list
            else:
                # A role change names the member: their page reloads, as its controls change.
                reload = event.type == EventType.BOARD_UPDATED and sub.user_id == event.user_id
                self._send(sub, _message(event, reload=reload))

    async def broadcast_resync(self) -> None:
        """Every socket reloads its board: events may have been missed (the listener lost its
        connection)."""
        message = json.dumps({"type": EventType.RESYNC})
        for sub in self.subscribers():
            self._send(sub, message)

    def _send(self, sub: Subscriber, message: str) -> bool:
        try:
            sub.queue.put_nowait(message)
        except asyncio.QueueFull:
            self._close_now(sub, CloseCode.TRY_AGAIN_LATER)
            return False
        return True

    def _close_after(self, sub: Subscriber, message: str, code: CloseCode) -> None:
        """Send ``message`` as the socket's last, then close it with ``code``."""
        if self._send(sub, message):
            sub.close(code)
            self.unsubscribe(sub)

    def _close_now(self, sub: Subscriber, code: CloseCode) -> None:
        """Close the socket with ``code`` without sending what is still queued."""
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.close(code)
        self.unsubscribe(sub)
