"""Clean-up: what is past its retention is deleted for good.

``purge`` deletes the archived boards and categories older than their owner's retention
(``users.archive_retention_days``; NULL keeps them), the tasks deleted more than
``SOFT_DELETE_PURGE_AFTER`` ago, the notifications older than ``RETENTION``, and then the files
on disk that no attachment refers to any more. Nobody can see any of these rows, so nothing here
makes a request wait: every delete is one statement, and the advisory lock is only tried, never
waited for. What the database cascades (a board's categories, tasks, checklists, attachment rows)
goes with its parent and is not counted. No realtime events are sent.

``PurgeTask`` runs it every ``EASYTODO_PURGE_INTERVAL_HOURS`` inside the app; the same function
is behind ``python -m easytodo.cli purge``. Several workers (or the CLI) may run it at once: one
at a time gets the lock, and the others do nothing.
"""

import asyncio
import logging
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import ColumnElement, Delete, Interval, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from easytodo.attachments.storage import AttachmentStore
from easytodo.content.service import SOFT_DELETE_PURGE_AFTER
from easytodo.models import Attachment, Board, Category, Item, Notification, User
from easytodo.notifications.service import RETENTION

log = logging.getLogger(__name__)

PURGE_LOCK_ID = 0x45544450  # "ETDP": the advisory lock that lets one purge run at a time
# A file with no attachment row is deleted once it is older than this: an upload writes the file
# before its row is committed.
ORPHAN_GRACE = timedelta(hours=1)
FIRST_RUN_DELAY_S = 300  # a task waits this long (or one interval, if shorter) before it runs
PURGE_BUSY = "Another purge is running. Try again later."


@dataclass(frozen=True)
class PurgeReport:
    """What a purge deleted. The rows are those it deleted itself, without what cascaded from
    them; ``files`` counts attachment files (keys), so a file with its thumbnail and preview is
    one, and so are the thumbnail and preview a cancelled upload left without their file."""

    boards: int
    categories: int
    items: int
    notifications: int
    files: int

    def summary(self) -> str:
        return (
            f"Purged: {self.boards} boards, {self.categories} categories, {self.items} items, "
            f"{self.notifications} notifications, {self.files} files."
        )


def _past_retention(archived_at: ColumnElement[datetime], now: datetime) -> ColumnElement[bool]:
    """``archived_at`` plus the retention of the user in the statement (``User``) is before
    ``now``; false while either is NULL. The retention is added in hours: ``+ N days`` counts in
    the zone of the session, and across a daylight saving change the purge would then be an hour
    off the moment the archive shows (``purge_at``, which is computed in UTC)."""
    retention = func.make_interval(0, 0, 0, 0, User.archive_retention_days * 24, type_=Interval)
    return archived_at + retention < now


async def purge(db: AsyncSession, store: AttachmentStore, *, now: datetime) -> PurgeReport | None:
    """Delete what is past its retention at ``now``, in one transaction, then the files no
    attachment refers to; ``None`` when another purge holds the lock (nothing is done then).

    The files are cleaned only if the database had an attachment when the purge began: a
    database without any, next to a volume with files, is an empty or a not yet restored one, and
    the files would all be taken for orphans (a restored volume keeps the files' old ages).
    """
    if not await db.scalar(select(func.pg_try_advisory_xact_lock(PURGE_LOCK_ID))):
        await db.rollback()
        return None
    # Before the rows go: the files of the attachments deleted now are to be cleaned too.
    had_attachments = await db.scalar(select(exists().select_from(Attachment)))
    boards = await _delete(
        db,
        delete(Board).where(Board.owner_id == User.id, _past_retention(Board.archived_at, now)),
    )
    categories = await _delete(  # the owner of the board decides, not whoever archived it
        db,
        delete(Category).where(
            Category.board_id == Board.id,
            Board.owner_id == User.id,
            _past_retention(Category.archived_at, now),
        ),
    )
    items = await _delete(db, delete(Item).where(Item.deleted_at < now - SOFT_DELETE_PURGE_AFTER))
    notifications = await _delete(
        db, delete(Notification).where(Notification.created_at < now - RETENTION)
    )
    await db.commit()
    files = await _clean_files(db, store, now, had_attachments=bool(had_attachments))
    return PurgeReport(boards, categories, items, notifications, files)


async def _delete(db: AsyncSession, statement: Delete) -> int:
    result = await db.execute(statement.execution_options(synchronize_session=False))
    return result.rowcount


async def _clean_files(
    db: AsyncSession, store: AttachmentStore, now: datetime, *, had_attachments: bool
) -> int:
    """Delete the files on disk that no attachment refers to and that are older than
    ``ORPHAN_GRACE``; how many keys."""
    if not had_attachments:
        kept = len(await asyncio.to_thread(lambda: list(store.keys())))
        if kept:
            log.warning("Purge kept %d attachment files: the database has no attachments.", kept)
        return 0
    in_use = set(await db.scalars(select(Attachment.storage_key)))
    # End the read here, so that no transaction stays open while the files are worked through (a
    # commit and not a rollback: that leaves the objects of the caller's session loaded).
    await db.commit()
    older_than = (now - ORPHAN_GRACE).timestamp()
    return await asyncio.to_thread(_delete_orphans, store, in_use, older_than)


def _delete_orphans(store: AttachmentStore, in_use: set[str], older_than: float) -> int:
    """Delete the files of keys not in ``in_use`` last written before ``older_than`` (a time
    stamp), with their thumbnails and previews; then the thumbnails and previews of files that
    are not there, likewise. Returns how many keys. A file that is gone meanwhile (another worker
    cleans too) is skipped."""
    removed = 0
    for key in set(store.keys()) - in_use:
        try:
            if store.path(key).stat().st_mtime >= older_than:
                continue  # an upload may be writing it, and its row is not committed yet
        except FileNotFoundError:
            continue
        store.delete(key)
        removed += 1
    derived: set[str] = set()
    for path in store.derived_orphans():
        # A row whose file is lost keeps its thumbnail, as it keeps its other data.
        if _key_of(path) in in_use:
            continue
        try:
            if path.stat().st_mtime >= older_than:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        derived.add(_key_of(path))
    return removed + len(derived)


def _key_of(derived_file: Path) -> str:
    return derived_file.name.split(".", 1)[0]


class PurgeTask:
    """Runs ``purge`` in the background every ``interval_s`` seconds, in a session of its own.
    The first run is ``FIRST_RUN_DELAY_S`` after ``start`` (sooner if the interval is shorter),
    and so is the retry after a failure, which is logged and never stops the task."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: AttachmentStore,
        interval_s: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._keep_purging())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _keep_purging(self) -> None:
        soon = min(self.interval_s, FIRST_RUN_DELAY_S)
        delay = soon
        while True:
            await asyncio.sleep(delay)
            try:
                await self._purge_once()
                delay = self.interval_s
            except Exception:
                log.exception("Purge failed; trying again in %g s.", soon)
                delay = soon

    async def _purge_once(self) -> None:
        async with self._sessionmaker() as db:
            report = await purge(db, self._store, now=datetime.now(UTC))
        # The app's logs are WARNING and above: this one says that data was deleted.
        if report is not None and any(astuple(report)):
            log.warning("%s", report.summary())
