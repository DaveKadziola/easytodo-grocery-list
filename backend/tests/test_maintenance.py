"""The clean-up: archived boards and categories past the owner's retention, tasks deleted more
than 10 minutes ago, notifications older than 30 days, and the files no row refers to any more."""

import asyncio
import inspect
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest
from asgi_lifespan import LifespanManager
from factories import (
    create_attachment,
    create_board,
    create_category,
    create_item,
    create_notification,
    create_user,
)
from helpers import count_queries
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from easytodo import maintenance
from easytodo.attachments.storage import AttachmentStore
from easytodo.boards.service import list_archived_boards
from easytodo.cli import main
from easytodo.content.service import list_archived_categories
from easytodo.main import create_app
from easytodo.maintenance import (
    FIRST_RUN_DELAY_S,
    PURGE_BUSY,
    PURGE_LOCK_ID,
    PURGE_LOCK_TIMEOUT,
    PurgeReport,
    PurgeTask,
    purge,
)
from easytodo.models import (
    Attachment,
    Board,
    BoardRole,
    Category,
    Item,
    Notification,
    User,
)


async def user_with(db, email: str, retention: int | None) -> User:
    """An account whose archive retention is ``retention`` days (None: never delete)."""
    user = await create_user(db, email)
    await db.execute(
        update(User).where(User.id == user.id).values(archive_retention_days=retention)
    )
    await db.commit()
    await db.refresh(user)
    return user


async def names(db, model) -> list[str]:
    return sorted(await db.scalars(select(model.name)))


async def count(db, model) -> int:
    return await db.scalar(select(func.count()).select_from(model))


async def deleted_at(db, item: Item, when: datetime) -> None:
    await db.execute(update(Item).where(Item.id == item.id).values(deleted_at=when))
    await db.commit()


async def created_at(db, notification: Notification, when: datetime) -> None:
    await db.execute(
        update(Notification).where(Notification.id == notification.id).values(created_at=when)
    )
    await db.commit()


def file_on_disk(
    store, *, key=None, main=True, thumb=False, preview=False, age=timedelta(hours=2)
) -> str:
    """The files of an attachment ``key`` (a new one unless given) as they were written ``age``
    ago: the file itself and, if asked for, its thumbnail and preview."""
    key = key or secrets.token_hex(16)
    wanted = [(store.path(key), main), (store.thumb_path(key), thumb)]
    wanted.append((store.preview_path(key), preview))
    modified = (datetime.now(UTC) - age).timestamp()
    for path, written in wanted:
        if written:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            os.utime(path, (modified, modified))
    return key


async def an_item(db) -> Item:
    """A task on an active board."""
    owner = await create_user(db, "keeper@example.com")
    return await create_item(db, await create_category(db, await create_board(db, owner, "Kept")))


async def a_row_with_a_file(db, store) -> str:
    """An attachment of a task on an active board, with its file; returns the key."""
    return (await create_attachment(db, await an_item(db), store=store)).storage_key


def test_the_summary_names_each_count():
    assert PurgeReport(1, 0, 2, 0, 3).summary() == (
        "Purged: 1 boards, 0 categories, 2 items, 0 notifications, 3 files."
    )


async def test_archived_boards_follow_their_owners_retention(db, store):
    week = await user_with(db, "week@example.com", 7)
    month = await user_with(db, "month@example.com", 30)
    never = await user_with(db, "never@example.com", None)
    now = datetime.now(UTC)
    await create_board(db, week, "Week", archived_at=now - timedelta(days=8))
    await create_board(db, month, "Month", archived_at=now - timedelta(days=8))
    await create_board(db, never, "Never", archived_at=now - timedelta(days=400))
    await create_board(db, week, "Active")

    assert await purge(db, store, now=now) == PurgeReport(1, 0, 0, 0, 0)
    assert await names(db, Board) == ["Active", "Month", "Never"]

    assert await purge(db, store, now=now + timedelta(days=23)) == PurgeReport(1, 0, 0, 0, 0)
    assert await names(db, Board) == ["Active", "Never"]


async def test_archived_categories_follow_the_board_owners_retention(db, store):
    week = await user_with(db, "week@example.com", 7)
    month = await user_with(db, "month@example.com", 30)
    long_editor = await user_with(db, "editor1@example.com", 365)
    short_editor = await user_with(db, "editor2@example.com", 7)
    now = datetime.now(UTC)
    weekly = await create_board(db, week, "Weekly", {long_editor: BoardRole.EDITOR})
    monthly = await create_board(db, month, "Monthly", {short_editor: BoardRole.EDITOR})
    old = now - timedelta(days=8)
    await create_category(db, weekly, "Gone", archived_at=old)
    await create_category(db, weekly, "Active 1")
    await create_category(db, monthly, "Kept", archived_at=old)
    await create_category(db, monthly, "Active 2")

    assert await purge(db, store, now=now) == PurgeReport(0, 1, 0, 0, 0)
    assert await names(db, Category) == ["Active 1", "Active 2", "Kept"]


async def test_purge_matches_the_dates_shown_in_the_archive(db, store):
    """The archive says "deleted on" a moment computed in UTC; across the change from summer
    time (25.10) an interval of days added in SQL, in the session's zone, is an hour off."""
    owner = await create_user(db, "dave@example.com")  # 30 days
    archived = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    await create_board(db, owner, "Old board", archived_at=archived)
    active = await create_board(db, owner, "Active")
    await create_category(db, active, "Old category", archived_at=archived)
    [shown_board] = await list_archived_boards(db, owner)
    _, _, [shown_category] = await list_archived_categories(db, active.id, owner)
    assert shown_board.purge_at == shown_category.purge_at == datetime(2026, 11, 19, 12, tzinfo=UTC)
    purge_at = shown_board.purge_at

    async def purge_in_warsaw(now: datetime) -> PurgeReport | None:
        # Same transaction: a SET of the session does not outlive a connection the pool closes.
        await db.execute(text("SET TIME ZONE 'Europe/Warsaw'"))
        return await purge(db, store, now=now)

    assert await purge_in_warsaw(purge_at - timedelta(minutes=1)) == PurgeReport(0, 0, 0, 0, 0)
    assert await count(db, Board) == 2 and await count(db, Category) == 1

    assert await purge_in_warsaw(purge_at + timedelta(minutes=1)) == PurgeReport(1, 1, 0, 0, 0)
    assert await names(db, Board) == ["Active"] and await count(db, Category) == 0


async def test_soft_deleted_items_go_after_10_minutes(db, store):
    owner = await create_user(db)
    category = await create_category(db, await create_board(db, owner))
    now = datetime.now(UTC)
    for name, deleted in (("Gone", timedelta(minutes=11)), ("Undoable", timedelta(minutes=9))):
        await deleted_at(db, await create_item(db, category, name), now - deleted)
    await create_item(db, category, "Active")

    assert await purge(db, store, now=now) == PurgeReport(0, 0, 1, 0, 0)
    assert await names(db, Item) == ["Active", "Undoable"]


async def test_notifications_go_after_30_days(db, store):
    user = await create_user(db)
    now = datetime.now(UTC)
    for read, age in ((True, 31), (False, 31), (False, 29)):
        notification = await create_notification(db, user, read=read)
        await created_at(db, notification, now - timedelta(days=age))

    assert await purge(db, store, now=now) == PurgeReport(0, 0, 0, 2, 0)
    assert await count(db, Notification) == 1


async def test_purge_runs_the_same_statements_whatever_it_deletes(db, store):
    """One statement per kind of row, never a loop over the rows: that keeps the time rows are
    locked short, whether a purge deletes none or hundreds."""
    owner = await user_with(db, "week@example.com", 7)
    now = datetime.now(UTC)
    with count_queries(db) as when_empty:
        assert await purge(db, store, now=now) == PurgeReport(0, 0, 0, 0, 0)
    active = await create_board(db, owner, "Active")
    for number in range(3):
        old = now - timedelta(days=8)
        await create_board(db, owner, f"Old {number}", archived_at=old)
        await create_category(db, active, f"Old {number}", archived_at=old)
        item = await create_item(db, await create_category(db, active, f"Active {number}"))
        await deleted_at(db, item, now - timedelta(minutes=11))
        await created_at(db, await create_notification(db, owner), now - timedelta(days=31))

    with count_queries(db) as when_busy:
        assert await purge(db, store, now=now) == PurgeReport(3, 3, 3, 3, 0)

    assert len(when_busy) == len(when_empty)


async def test_purged_rows_take_their_files_with_them(db, store):
    owner = await user_with(db, "week@example.com", 7)
    archived = datetime.now(UTC) - timedelta(days=8)
    board = await create_board(db, owner, "Old", archived_at=archived)
    item = await create_item(db, await create_category(db, board))
    attachment = await create_attachment(db, item, store=store)
    assert store.path(attachment.storage_key).is_file()

    # The file was written just now: it goes once it is older than ORPHAN_GRACE.
    report = await purge(db, store, now=datetime.now(UTC) + timedelta(hours=2))

    assert report == PurgeReport(1, 0, 0, 0, 1)  # the category and the task went with the board
    assert not store.path(attachment.storage_key).exists()
    assert await count(db, Attachment) == 0


async def test_only_old_orphan_files_are_removed(db, store):
    kept = await a_row_with_a_file(db, store)
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(store.path(kept), (old, old))  # old, but a row refers to it
    orphan = file_on_disk(store, thumb=True)
    fresh = file_on_disk(store, age=timedelta(minutes=5))

    report = await purge(db, store, now=datetime.now(UTC))

    assert report == PurgeReport(0, 0, 0, 0, 1)
    assert store.path(kept).is_file() and store.path(fresh).is_file()
    assert not store.path(orphan).exists() and not store.thumb_path(orphan).exists()


async def test_derived_files_left_without_their_file_are_removed_after_the_grace_period(db, store):
    """A cancelled upload can leave the thumbnail and the preview of a file that is gone; the
    listing of the store never names them. They count once, as the file they belonged to."""
    item = await an_item(db)
    kept = (await create_attachment(db, item, store=store)).storage_key
    store.thumb_path(kept).write_bytes(b"thumb")  # beside a file a row refers to
    lost = (await create_attachment(db, item)).storage_key  # a row whose file is gone
    file_on_disk(store, key=lost, main=False, thumb=True)
    pair = file_on_disk(store, main=False, thumb=True, preview=True)
    single = file_on_disk(store, main=False, thumb=True)
    fresh = file_on_disk(store, main=False, thumb=True, age=timedelta(minutes=5))

    report = await purge(db, store, now=datetime.now(UTC))

    assert report == PurgeReport(0, 0, 0, 0, 2)
    for key in (pair, single):
        assert not store.thumb_path(key).exists() and not store.preview_path(key).exists()
    for key in (kept, lost, fresh):  # a row refers to it, or it may belong to an upload in flight
        assert store.thumb_path(key).is_file()


async def test_files_are_kept_when_the_database_has_no_attachments(db, store, caplog):
    """An empty (or not yet restored) database next to a volume full of files: not a reason to
    delete the lot."""
    orphan = file_on_disk(store, thumb=True)
    derived = file_on_disk(store, main=False, thumb=True)

    with caplog.at_level(logging.WARNING):
        report = await purge(db, store, now=datetime.now(UTC))

    assert report == PurgeReport(0, 0, 0, 0, 0)
    assert store.path(orphan).is_file() and store.thumb_path(derived).is_file()
    assert "Purge kept 1 attachment files: the database has no attachments." in caplog.text


async def test_a_second_concurrent_purge_does_nothing(db, store, _db_engine):
    user = await create_user(db)
    now = datetime.now(UTC)
    await created_at(db, await create_notification(db, user), now - timedelta(days=31))

    async with _db_engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": PURGE_LOCK_ID})
        assert await purge(db, store, now=now) is None
        assert await count(db, Notification) == 1
        await other.rollback()

    assert await purge(db, store, now=now) == PurgeReport(0, 0, 0, 1, 0)
    assert await count(db, Notification) == 0


async def test_cli_purge(db, cli_env, store, capsys, _db_engine):
    owner = await create_user(db)
    category = await create_category(db, await create_board(db, owner))
    now = datetime.now(UTC)
    await deleted_at(db, await create_item(db, category, "Gone"), now - timedelta(minutes=11))
    await created_at(db, await create_notification(db, owner), now - timedelta(days=31))
    # A task with a file keeps it, and with a row in the database the files get cleaned up.
    kept = await create_attachment(db, await create_item(db, category, "Kept"), store=store)
    orphan = file_on_disk(store)

    assert await asyncio.to_thread(main, ["purge"]) == 0

    assert capsys.readouterr().out == (
        "Purged: 0 boards, 0 categories, 1 items, 1 notifications, 1 files.\n"
    )
    assert store.path(kept.storage_key).is_file() and not store.path(orphan).exists()

    async with _db_engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": PURGE_LOCK_ID})
        assert await asyncio.to_thread(main, ["purge"]) == 1
        await other.rollback()
    assert capsys.readouterr().err == f"error: {PURGE_BUSY}\n"
    assert PURGE_BUSY == "Another purge is running. Try again later."


async def test_cli_purge_reports_a_lock_it_waited_too_long_for(
    db, cli_env, store, capsys, _db_engine, monkeypatch
):
    monkeypatch.setattr(maintenance, "LOCK_TIMEOUT_S", 0.3)
    owner = await user_with(db, "week@example.com", 7)
    expired = await create_board(
        db, owner, "Old", archived_at=datetime.now(UTC) - timedelta(days=8)
    )

    async with _db_engine.connect() as other:
        await other.execute(
            text("SELECT id FROM boards WHERE id = :id FOR UPDATE"), {"id": expired.id}
        )
        assert await asyncio.to_thread(main, ["purge"]) == 1
        await other.rollback()

    out, err = capsys.readouterr()
    assert (out, err) == ("", "error: The purge waited too long for a lock. Try again later.\n")
    assert PURGE_LOCK_TIMEOUT == "The purge waited too long for a lock. Try again later."
    assert await names(db, Board) == ["Old"]  # nothing was deleted


async def test_cli_purge_does_not_hide_other_database_errors(db, cli_env, store, monkeypatch):
    async def purge_that_breaks(*args, **kwargs):
        raise DBAPIError("SELECT 1", {}, Exception("something else"))

    monkeypatch.setattr("easytodo.cli.purge", purge_that_breaks)

    with pytest.raises(DBAPIError):
        await asyncio.to_thread(main, ["purge"])


async def wait_until(condition, seconds: float = 2) -> None:
    """Return once ``condition()`` (a function, or a coroutine function) is true; fail after
    ``seconds``."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        met = condition()
        if inspect.isawaitable(met):
            met = await met
        if met:
            return
        assert asyncio.get_running_loop().time() < deadline, "the condition never came true"
        await asyncio.sleep(0.02)


async def test_purge_task_runs_on_its_interval_and_survives_errors(
    db, store, _db_engine, monkeypatch, caplog
):
    user = await create_user(db)
    await created_at(
        db, await create_notification(db, user), datetime.now(UTC) - timedelta(days=31)
    )
    real_purge = maintenance.purge
    calls = []

    async def purge_that_fails_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ConnectionRefusedError("the database is not up yet")
        return await real_purge(*args, **kwargs)

    monkeypatch.setattr(maintenance, "purge", purge_that_fails_once)
    task = PurgeTask(async_sessionmaker(_db_engine, expire_on_commit=False), store, 0.05)

    summary = "Purged: 0 boards, 0 categories, 0 items, 1 notifications, 0 files."

    with caplog.at_level(logging.WARNING):
        await task.start()
        try:
            assert task.running
            # The line, not the vanished row: it comes after the commit, from the file work.
            await wait_until(lambda: summary in caplog.text)
        finally:
            await task.stop()

    assert task.running is False
    assert "Purge failed" in caplog.text
    assert await count(db, Notification) == 0
    assert len(calls) >= 2


async def test_purge_task_waits_5_minutes_before_its_first_run_and_before_a_retry(
    _db_engine, monkeypatch
):
    sleeps = []

    async def quick_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0 if len(sleeps) < 4 else 3600)  # the fourth is never over

    outcomes = iter([PurgeReport(0, 0, 0, 0, 0), ConnectionRefusedError("down")])

    async def purge_that_fails_once(db, store, *, now):
        outcome = next(outcomes, None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(maintenance, "purge", purge_that_fails_once)
    task = PurgeTask(async_sessionmaker(_db_engine), None, 86400, sleep=quick_sleep)

    await task.start()
    try:
        await wait_until(lambda: len(sleeps) == 4)
    finally:
        await task.stop()

    assert sleeps == [FIRST_RUN_DELAY_S, 86400, FIRST_RUN_DELAY_S, 86400]
    assert FIRST_RUN_DELAY_S == 300


async def test_a_run_that_deletes_nothing_logs_nothing(db, store, _db_engine, caplog):
    sleeps = []

    async def quick_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0 if len(sleeps) < 4 else 3600)  # three runs, then it waits

    task = PurgeTask(async_sessionmaker(_db_engine), store, 86400, sleep=quick_sleep)

    with caplog.at_level(logging.INFO):  # not even below WARNING
        await task.start()
        try:
            await wait_until(lambda: len(sleeps) == 4)
        finally:
            await task.stop()

    assert [r for r in caplog.records if r.name == maintenance.log.name] == []


async def test_a_file_that_cannot_be_deleted_does_not_stop_the_others(
    db, store, monkeypatch, caplog
):
    """One file with the wrong permissions: it is named in the log and left, the rest of the
    files are cleaned, and what the database part deleted is still in the report."""
    await a_row_with_a_file(db, store)
    user = await create_user(db)
    await created_at(
        db, await create_notification(db, user), datetime.now(UTC) - timedelta(days=31)
    )
    stuck, gone_1, gone_2 = (file_on_disk(store) for _ in range(3))
    stuck_derived = file_on_disk(store, main=False, thumb=True)
    gone_derived = file_on_disk(store, main=False, thumb=True)
    real_delete, real_delete_derived = AttachmentStore.delete, AttachmentStore.delete_derived

    def delete(self, key):
        if key == stuck:
            raise PermissionError(13, "Permission denied")
        return real_delete(self, key)

    def delete_derived(self, path):
        if path == store.thumb_path(stuck_derived):
            raise PermissionError(13, "Permission denied")
        return real_delete_derived(self, path)

    monkeypatch.setattr(AttachmentStore, "delete", delete)
    monkeypatch.setattr(AttachmentStore, "delete_derived", delete_derived)

    with caplog.at_level(logging.WARNING):
        report = await purge(db, store, now=datetime.now(UTC))

    assert report == PurgeReport(0, 0, 0, 1, 3)  # the two files and the derived pair of one key
    assert store.path(stuck).is_file() and store.thumb_path(stuck_derived).is_file()
    assert not store.path(gone_1).exists() and not store.path(gone_2).exists()
    assert not store.thumb_path(gone_derived).exists()
    warnings = [r.getMessage() for r in caplog.records if "could not delete" in r.getMessage()]
    assert len(warnings) == 2
    assert f"{store.path(stuck)}: [Errno 13] Permission denied" in warnings[0] + warnings[1]
    assert str(store.thumb_path(stuck_derived)) in warnings[0] + warnings[1]


async def test_files_that_vanish_during_the_purge_are_skipped_and_not_counted(
    db, store, monkeypatch, caplog
):
    """Another worker (or the CLI) cleans the same files at the same time: whoever deleted a
    file counts it, and a file that is gone is no failure."""
    await a_row_with_a_file(db, store)
    mine, taken = file_on_disk(store), file_on_disk(store)
    mine_derived = file_on_disk(store, main=False, thumb=True)
    taken_derived = file_on_disk(store, main=False, thumb=True)
    unlisted = secrets.token_hex(16)  # in the listing, gone before its age is read
    real_keys = AttachmentStore.keys
    real_delete, real_delete_derived = AttachmentStore.delete, AttachmentStore.delete_derived

    def keys(self):
        yield from real_keys(self)
        yield unlisted

    def delete(self, key):
        if key == taken:  # the other sweep is quicker, after this one has read the age
            real_delete(self, key)
        return real_delete(self, key)

    def delete_derived(self, path):
        if path == store.thumb_path(taken_derived):
            real_delete_derived(self, path)
        return real_delete_derived(self, path)

    monkeypatch.setattr(AttachmentStore, "keys", keys)
    monkeypatch.setattr(AttachmentStore, "delete", delete)
    monkeypatch.setattr(AttachmentStore, "delete_derived", delete_derived)

    with caplog.at_level(logging.WARNING):
        report = await purge(db, store, now=datetime.now(UTC))

    assert report == PurgeReport(0, 0, 0, 0, 2)  # mine and mine_derived
    assert not store.path(mine).exists() and not store.thumb_path(mine_derived).exists()
    assert not store.path(taken).exists() and not store.thumb_path(taken_derived).exists()
    assert "could not delete" not in caplog.text


async def test_a_row_lock_the_purge_cannot_get_fails_the_run_and_deletes_nothing(
    db, store, _db_engine, monkeypatch
):
    """A transaction stuck on a row the purge must delete: the run fails after the lock timeout,
    instead of holding its connection and the advisory lock for as long as the other lasts."""
    monkeypatch.setattr(maintenance, "LOCK_TIMEOUT_S", 0.3)
    owner = await user_with(db, "week@example.com", 7)
    now = datetime.now(UTC)
    await create_board(db, owner, "Old", archived_at=now - timedelta(days=8))  # deleted first
    category = await create_category(db, await create_board(db, owner, "Active"))
    stuck = await create_item(db, category)
    await deleted_at(db, stuck, now - timedelta(minutes=11))

    async with _db_engine.connect() as other:
        await other.execute(
            text("SELECT id FROM items WHERE id = :id FOR UPDATE"), {"id": stuck.id}
        )
        started = time.monotonic()
        with pytest.raises(DBAPIError, match="lock timeout"):
            await purge(db, store, now=now)
        assert time.monotonic() - started < 2
        await db.rollback()
        assert await names(db, Board) == ["Active", "Old"]  # not half done: the board is there
        await other.rollback()

    assert await purge(db, store, now=now) == PurgeReport(1, 0, 1, 0, 0)


async def test_the_purge_task_survives_a_row_lock_it_cannot_get(
    db, store, _db_engine, monkeypatch, caplog
):
    monkeypatch.setattr(maintenance, "LOCK_TIMEOUT_S", 0.2)
    owner = await create_user(db)
    category = await create_category(db, await create_board(db, owner))
    stuck = await create_item(db, category)
    await deleted_at(db, stuck, datetime.now(UTC) - timedelta(minutes=11))
    task = PurgeTask(async_sessionmaker(_db_engine, expire_on_commit=False), store, 0.05)

    async with _db_engine.connect() as other:
        await other.execute(
            text("SELECT id FROM items WHERE id = :id FOR UPDATE"), {"id": stuck.id}
        )
        with caplog.at_level(logging.WARNING):
            await task.start()
            try:
                await wait_until(lambda: "Purge failed" in caplog.text)
                assert task.running
                await other.rollback()  # the other transaction ends
                await wait_until(lambda: "0 categories, 1 items" in caplog.text)
            finally:
                await task.stop()

    assert await count(db, Item) == 0


async def test_lifespan_starts_the_purge_task_only_when_enabled(app, _running_app, settings):
    assert app.state.purge_task is None

    enabled = create_app(settings.model_copy(update={"purge_interval_hours": 24}))
    async with LifespanManager(enabled):
        task = enabled.state.purge_task
        assert task.interval_s == 86400
        assert task.running
    assert not task.running
