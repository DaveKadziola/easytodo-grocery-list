"""Notifications: a line for a person about a change somebody else made that they would otherwise
meet by surprise (a board they were on has gone, a role changed, a category moved away).

A notification is a row, added by ``notify`` in the transaction of the change that causes it, so
the two stand or fall together; the caller commits both with ``commit_and_publish(db, ...,
*events)``, the events being what ``notify`` returns: they tell the person's open pages (the
bell's badge and panel) to look again. The badge and the panel show the last ``RETENTION`` only
(the database says when that is, so a purge that is late, or turned off, changes nothing on
screen).

Reading is the person's own: ``mark_all_read`` clears their badge and tells their other devices.
"""

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import BigInteger, bindparam, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.models import BoardMember, Notification, NotificationKind, User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish

RETENTION = timedelta(days=30)  # how long a notification is shown (the purge deletes it later)
MESSAGE_MAX_LENGTH = 600  # notifications.message is String(600)
PANEL_SIZE = 20  # how many the panel lists

# What each kind says: the names are those of the people and boards, as they were at the time.
MESSAGES: dict[NotificationKind, str] = {
    NotificationKind.MEMBER_JOINED: "{actor} joined “{board}”.",
    NotificationKind.MEMBER_LEFT: "{actor} left “{board}”.",
    NotificationKind.MEMBER_REMOVED: "{actor} removed you from “{board}”.",
    NotificationKind.ROLE_CHANGED: "{actor} changed your role on “{board}” to {role}.",
    NotificationKind.BOARD_ARCHIVED: "{actor} archived “{board}”.",
    NotificationKind.BOARD_RESTORED: "{actor} restored “{board}”.",
    NotificationKind.CATEGORY_MOVED: "{actor} moved “{category}” from “{source}” to “{target}”.",
}

# Built once, like the statements on the other hot paths: the bell asks for its badge on every
# page, and building a statement takes longer than running it. Not "func.now() - RETENTION" in
# Python: the database's clock decides what is recent.
_USER_ID = bindparam("user_id", type_=BigInteger)
_RECENT = Notification.created_at > func.now() - RETENTION
_UNREAD_COUNT = (
    select(func.count())
    .select_from(Notification)
    .where(Notification.user_id == _USER_ID, Notification.read_at.is_(None), _RECENT)
)
_RECENT_LIST = (
    select(Notification)
    .where(Notification.user_id == _USER_ID, _RECENT)
    .order_by(Notification.created_at.desc(), Notification.id.desc())
    .limit(bindparam("limit"))
)
# The parameter of an update cannot be named like a column ("user_id").
_MARK_READ = (
    update(Notification)
    .where(
        Notification.user_id == bindparam("reader_id", type_=BigInteger),
        Notification.read_at.is_(None),
    )
    .values(read_at=func.now())
    .returning(Notification.id)
    .execution_options(synchronize_session=False)
)
_MEMBER_IDS = (
    select(BoardMember.user_id)
    .where(BoardMember.board_id.in_(bindparam("board_ids", expanding=True)))
    .where(BoardMember.user_id != bindparam("exclude", type_=BigInteger))
    .distinct()
    .order_by(BoardMember.user_id)
)


def notify(
    db: AsyncSession,
    recipients: Iterable[int],
    kind: NotificationKind,
    *,
    board_id: int | None,
    **names: str,
) -> list[BoardEvent]:
    """Add a notification of ``kind`` for each of ``recipients`` (once each) to ``db``'s
    transaction, worded from ``names`` (``MESSAGES``), with the board it links to (``board_id``;
    None: there is none to open). Not committed: ``commit_and_publish`` the returned events,
    which tell the recipients' pages, together with the change.

    A message over ``MESSAGE_MAX_LENGTH`` is cut short and ends in "…" (names may be long).
    """
    people = list(dict.fromkeys(recipients))
    message = MESSAGES[kind].format(**names)
    if len(message) > MESSAGE_MAX_LENGTH:
        message = message[: MESSAGE_MAX_LENGTH - 1] + "…"
    db.add_all(
        Notification(user_id=person, kind=kind, message=message, board_id=board_id)
        for person in people
    )
    return [BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=person) for person in people]


async def member_ids(db: AsyncSession, *board_ids: int, exclude: int) -> list[int]:
    """The ids of the members of any of these boards, once each and in order, except ``exclude``
    (whoever made the change: they know). A plain read, without a lock."""
    return list(await db.scalars(_MEMBER_IDS, {"board_ids": list(board_ids), "exclude": exclude}))


async def unread_count(db: AsyncSession, user: User) -> int:
    """How many notifications of the last ``RETENTION`` ``user`` has not read: one statement."""
    return await db.scalar(_UNREAD_COUNT, {"user_id": user.id}) or 0


async def recent(db: AsyncSession, user: User, limit: int = PANEL_SIZE) -> list[Notification]:
    """``user``'s notifications of the last ``RETENTION``, the newest first, read ones too: one
    statement."""
    return list(await db.scalars(_RECENT_LIST, {"user_id": user.id, "limit": limit}))


async def mark_all_read(db: AsyncSession, user: User) -> None:
    """Mark everything ``user`` has been told as read (committed). Only the reader's own badge is
    cleared. If anything was unread, their other devices are told to show it
    (``NOTIFICATIONS_CHANGED``; the tab that read is skipped, as it knows)."""
    marked = (await db.scalars(_MARK_READ, {"reader_id": user.id})).all()
    if marked:
        await commit_and_publish(db, BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=user.id))
    else:
        await db.commit()
