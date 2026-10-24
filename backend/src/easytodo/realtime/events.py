"""The board event contract.

Events are thin: a type plus ids. A client that receives one fetches the HTML it needs through
the normal, authorised endpoints, so viewers and editors get their own markup without separate
channels. ``origin`` is the browser tab (``X-Client-Id``) whose request caused the event, so that
tab can be skipped: it already shows the change.

Every service that changes a board's data commits through ``commit_and_publish``, so its events
leave only together with the commit: each is a ``pg_notify`` on ``board_events`` inside the
transaction, which every worker's ``realtime.listener`` hands to its ``realtime.hub``. A socket
gets the event as ``{"type", "board_id", "category_ids"}``, plus ``"item_ids"`` when there are some
(the items whose details changed: a page with one of them open fetches its details again); the hub
decides which sockets get it. ``NOTIFICATIONS_CHANGED`` has no board: it names a user (``user_id``)
and goes to that user's own sockets (``/ws/user``) as just ``{"type"}``, the page asking for the
bell's new numbers itself.
"""

import json
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NOTIFY_CHANNEL = "board_events"  # the channel events are sent on (and listened for)
MAX_PAYLOAD_BYTES = 8000  # PostgreSQL's limit for a NOTIFY payload
_NOTIFY = text("SELECT pg_notify(:channel, :p)")


class EventType(StrEnum):
    BOARD_UPDATED = "board.updated"
    BOARD_DELETED = "board.deleted"
    CATEGORY_CHANGED = "category.changed"
    CATEGORY_DELETED = "category.deleted"
    CATEGORIES_REORDERED = "categories.reordered"
    MEMBER_REMOVED = "member.removed"
    SESSIONS_REVOKED = "sessions.revoked"
    NOTIFICATIONS_CHANGED = "notifications.changed"
    RESYNC = "resync"


@dataclass(frozen=True)
class BoardEvent:
    type: EventType
    board_id: int | None = None
    category_ids: tuple[int, ...] = ()
    item_ids: tuple[int, ...] = ()
    user_id: int | None = None
    session_ids: tuple[int, ...] = ()
    origin: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> Self:
        data = json.loads(s)
        return cls(
            type=EventType(data["type"]),
            board_id=data.get("board_id"),
            category_ids=tuple(data.get("category_ids", ())),
            item_ids=tuple(data.get("item_ids", ())),
            user_id=data.get("user_id"),
            session_ids=tuple(data.get("session_ids", ())),
            origin=data.get("origin"),
        )


# The X-Client-Id of the request being handled; set by middleware.ClientIdMiddleware.
current_origin: ContextVar[str | None] = ContextVar("current_origin", default=None)


async def commit_and_publish(db: AsyncSession, *events: BoardEvent) -> None:
    """Commit ``db``'s transaction together with ``events``.

    Each event is notified inside the transaction, so listeners get it only once the commit
    succeeds, and never after a rollback. An event without an origin gets the request's
    (``current_origin``).
    """
    origin = current_origin.get()
    for event in events:
        if event.origin is None:
            event = replace(event, origin=origin)
        for payload in _payloads(event):
            await db.execute(_NOTIFY, {"channel": NOTIFY_CHANNEL, "p": payload})
    await db.commit()


def _payloads(event: BoardEvent) -> Iterator[str]:
    """``event`` as JSON; one too large for a notification goes as several events, each with
    part of its longest id list (many revoked sessions, say)."""
    payload = event.to_json()
    if len(payload.encode()) < MAX_PAYLOAD_BYTES:
        yield payload
        return
    field = max(("session_ids", "category_ids", "item_ids"), key=lambda n: len(getattr(event, n)))
    ids = getattr(event, field)
    if len(ids) < 2:
        raise ValueError(f"A {event.type} event does not fit in a notification.")
    half = len(ids) // 2
    yield from _payloads(replace(event, **{field: ids[:half]}))
    yield from _payloads(replace(event, **{field: ids[half:]}))
