"""The board event contract.

Events are thin: a type plus ids. A client that receives one fetches the HTML it needs through
the normal, authorised endpoints, so viewers and editors get their own markup without separate
channels. ``origin`` is the browser tab (``X-Client-Id``) whose request caused the event, so that
tab can be skipped: it already shows the change.

Every service that changes a board's data commits through ``commit_and_publish``, so its events
leave only together with the commit.
"""

import json
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession


class EventType(StrEnum):
    BOARD_UPDATED = "board.updated"
    BOARD_DELETED = "board.deleted"
    CATEGORY_CHANGED = "category.changed"
    CATEGORY_DELETED = "category.deleted"
    CATEGORIES_REORDERED = "categories.reordered"
    MEMBER_REMOVED = "member.removed"
    SESSIONS_REVOKED = "sessions.revoked"
    RESYNC = "resync"


@dataclass(frozen=True)
class BoardEvent:
    type: EventType
    board_id: int | None = None
    category_ids: tuple[int, ...] = ()
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
            user_id=data.get("user_id"),
            session_ids=tuple(data.get("session_ids", ())),
            origin=data.get("origin"),
        )


# The X-Client-Id of the request being handled; set by middleware.ClientIdMiddleware.
current_origin: ContextVar[str | None] = ContextVar("current_origin", default=None)


async def commit_and_publish(db: AsyncSession, *events: BoardEvent) -> None:
    """Commit ``db``'s transaction together with ``events``.

    For now this only commits; delivering the events (``pg_notify`` inside the same
    transaction) is not wired up yet.
    """
    await db.commit()
