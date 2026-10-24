"""The sockets of one worker, and which of them get each event.

Every board page holds a WebSocket (``realtime.routes``) subscribed here as a ``Subscriber`` with
its board, and every signed-in page one for its user's notifications, a ``Subscriber`` without a
board (``board_id=None``). ``dispatch`` queues an event's message for the sockets that need it, or
has a socket closed: the page then reconnects (and reloads the board), signs in again or leaves
the board, by the close code. The hub keeps only memory state: it never reads the database.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from easytodo.realtime.events import BoardEvent, EventType

QUEUE_SIZE = 100  # messages a socket may fall behind before it is closed
# How many sockets one session may hold at a time on one worker (the hub keeps only its own
# worker's count, so with several workers a session may hold that many on each), board pages and
# user sockets together: a script with one cookie cannot use up the server's file descriptors. A
# tab that finds the budget spent is closed with 1013 and tries again later.
MAX_SOCKETS_PER_SESSION = 30


class CloseCode(IntEnum):
    TRY_AGAIN_LATER = 1013  # fell behind, or events were missed: reconnect, which reloads the board
    WRONG_ORIGIN = 4400  # a page at another address than EASYTODO_PUBLIC_URL: not retried
    NOT_AUTHENTICATED = 4401  # signed out, or the session was revoked
    FORBIDDEN = 4403  # no longer a member
    NOT_FOUND = 4404  # the board was deleted, or the user is not a member of it


@dataclass(eq=False)  # each socket is its own subscriber, however alike
class Subscriber:
    board_id: int | None  # None: the user's own socket (notifications), not a board page's
    user_id: int
    session_id: int
    client_id: str | None  # the browser tab (X-Client-Id), whose own changes it does not get
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    close_code: int | None = None  # set when the hub closes the socket
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    # When the session ends (a session's end does not move), for the socket's own timer.
    expires_at: datetime | None = None

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
    if event.item_ids:
        message["item_ids"] = list(event.item_ids)
    if reload:
        message["reload"] = True
    return json.dumps(message)


class Hub:
    def __init__(self) -> None:
        self._boards: dict[int, set[Subscriber]] = {}  # the board pages, by board
        self._users: dict[int, set[Subscriber]] = {}  # the user sockets, by user
        self._per_session: dict[int, int] = {}  # how many sockets each session holds

    def _group(self, sub: Subscriber) -> tuple[dict[int, set[Subscriber]], int]:
        """The table ``sub`` belongs to, and its key in it."""
        if sub.board_id is None:
            return self._users, sub.user_id
        return self._boards, sub.board_id

    def subscribe(self, sub: Subscriber) -> bool:
        """Start sending ``sub`` events. ``False``, and nothing subscribed, when its session
        already holds ``MAX_SOCKETS_PER_SESSION`` sockets."""
        table, key = self._group(sub)
        if sub in table.get(key, ()):
            return True
        if self._per_session.get(sub.session_id, 0) >= MAX_SOCKETS_PER_SESSION:
            return False
        table.setdefault(key, set()).add(sub)
        self._per_session[sub.session_id] = self._per_session.get(sub.session_id, 0) + 1
        return True

    def unsubscribe(self, sub: Subscriber) -> None:
        """Stop sending ``sub`` events. Safe to repeat: the hub unsubscribes a socket it closes,
        and the route does so again on its way out, which must not give back a second place."""
        table, key = self._group(sub)
        subscribers = table.get(key)
        if subscribers is None or sub not in subscribers:
            return
        subscribers.discard(sub)
        if not subscribers:
            del table[key]
        held = self._per_session[sub.session_id] - 1
        if held:
            self._per_session[sub.session_id] = held
        else:
            del self._per_session[sub.session_id]

    def subscribers(self) -> list[Subscriber]:
        """Every socket subscribed, board pages and user sockets."""
        return [
            sub
            for table in (self._boards, self._users)
            for subscribers in table.values()
            for sub in subscribers
        ]

    async def dispatch(self, event: BoardEvent) -> None:
        if event.type == EventType.SESSIONS_REVOKED:
            revoked = set(event.session_ids)
            for sub in self.subscribers():
                if sub.session_id in revoked:
                    self._close_now(sub, CloseCode.NOT_AUTHENTICATED)
            return
        if event.type == EventType.NOTIFICATIONS_CHANGED:
            # The user's own sockets, but not the tab whose request made the change.
            sockets = self._users.get(event.user_id, ()) if event.user_id is not None else ()
            for sub in list(sockets):
                if event.origin is None or sub.client_id != event.origin:
                    self._send(sub, json.dumps({"type": event.type}))
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

    async def reconnect_all(self) -> None:
        """Close every socket with 1013: the listener lost its connection, so events may have
        been missed, revocations and member removals among them. Each page reconnects and
        reloads its board (or its bell), and the reconnect checks its session and membership
        again (a mere "resync" message would leave a socket open that the server meant to
        close)."""
        for sub in self.subscribers():
            self._close_now(sub, CloseCode.TRY_AGAIN_LATER)

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
