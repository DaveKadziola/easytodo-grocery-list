"""The clean-up: archived boards and categories past the owner's retention, tasks deleted more
than 10 minutes ago, notifications older than 30 days, and the files no row refers to any more."""

import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

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
from sqlalchemy.ext.asyncio import async_sessionmaker

from easytodo import maintenance
from easytodo.boards.service import list_archived_boards
from easytodo.cli import main
from easytodo.content.service import list_archived_categories
from easytodo.main import create_app
from easytodo.maintenance import (
    FIRST_RUN_DELAY_S,
    PURGE_BUSY,
    PURGE_LOCK_ID,
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

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


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


async def wait_until(condition, seconds: float = 2) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not await condition():
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

    async def notifications_gone() -> bool:
        return await count(db, Notification) == 0

    with caplog.at_level(logging.WARNING):
        await task.start()
        try:
            assert task.running
            await wait_until(notifications_gone)
        finally:
            await task.stop()

    assert task.running is False
    assert "Purge failed" in caplog.text
    assert "Purged: 0 boards, 0 categories, 0 items, 1 notifications, 0 files." in caplog.text
    assert len(calls) >= 2


async def test_purge_task_waits_5_minutes_before_its_first_run_and_before_a_retry(
    _db_engine, monkeypatch
):
    sleeps = []
    real_sleep = asyncio.sleep

    async def quick_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    outcomes = iter([PurgeReport(0, 0, 0, 0, 0), ConnectionRefusedError("down")])

    async def purge_that_fails_once(db, store, *, now):
        outcome = next(outcomes, None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(maintenance, "purge", purge_that_fails_once)
    monkeypatch.setattr(maintenance.asyncio, "sleep", quick_sleep)
    task = PurgeTask(async_sessionmaker(_db_engine), None, 86400)

    await task.start()
    try:
        while len(sleeps) < 4:
            await real_sleep(0.01)
    finally:
        await task.stop()

    assert sleeps[:4] == [FIRST_RUN_DELAY_S, 86400, FIRST_RUN_DELAY_S, 86400]
    assert FIRST_RUN_DELAY_S == 300


async def test_lifespan_starts_the_purge_task_only_when_enabled(app, _running_app, settings):
    assert app.state.purge_task is None

    enabled = create_app(settings.model_copy(update={"purge_interval_hours": 24}))
    async with LifespanManager(enabled):
        task = enabled.state.purge_task
        assert task.interval_s == 86400
        assert task.running
    assert not task.running
