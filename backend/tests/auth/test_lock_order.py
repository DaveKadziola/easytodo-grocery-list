"""One lock order per account: whatever writes more than one of an account's rows across
``users``, ``password_reset_tokens`` and ``user_sessions`` locks the ``users`` row first, so a
password change, a reset, an admin disabling or deleting the account and "sign out other devices"
queue on it instead of waiting for each other's rows (a deadlock, SQLSTATE 40P01, is a 500).

Each test stops one transaction at a chosen call, starts the other, waits until a query waits for
a lock (``wait_until_a_query_waits_for_a_lock``), and lets the first go on. No sleeps.
"""

import asyncio

import pytest
from factories import DEFAULT_PASSWORD, create_reset_token, create_user
from helpers import count_queries, signed_in, wait_until_a_query_waits_for_a_lock
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.admin import service as admin_service
from easytodo.auth import account_routes, reset
from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.models import PasswordResetToken, User, UserSession, UserStatus
from easytodo.security.passwords import verify_password
from easytodo.security.tokens import hash_token
from easytodo.web import FLASH_COOKIE

EMAIL = "dave@example.com"
NEW_PASSWORD = "an-even-better-passphrase"
CHANGE = {
    "current_password": DEFAULT_PASSWORD,
    "new_password": NEW_PASSWORD,
    "new_password_confirm": NEW_PASSWORD,
}


@pytest.fixture
async def spawn():
    """``spawn(coroutine)`` starts a task. At the end of the test, a failing one too, what still
    runs is cancelled and awaited, so that no transaction is left holding locks for the clean-up
    of the database (which would wait for them for good)."""
    tasks: list[asyncio.Task] = []

    def start(coroutine) -> asyncio.Task:
        tasks.append(asyncio.create_task(coroutine))
        return tasks[-1]

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def pause(monkeypatch, module, name, *, before: bool = False):
    """The first call of ``module.name`` stops just after it ran (just before it, with
    ``before=True``) until ``go`` is set; ``reached`` is set when it stops. Later calls pass
    through. Returns ``(reached, go)``."""
    reached, go, calls = asyncio.Event(), asyncio.Event(), []
    real = getattr(module, name)

    async def paused(*args, **kwargs):
        first = not calls
        calls.append(1)
        if first and before:
            reached.set()
            await go.wait()
        result = await real(*args, **kwargs)
        if first and not before:
            reached.set()
            await go.wait()
        return result

    monkeypatch.setattr(module, name, paused)
    return reached, go


async def run_blocked_by(db, go: asyncio.Event, *tasks):
    """Wait until a query waits for a lock the paused transaction holds, let that one go on, and
    return what ``tasks`` return (a deadlock would raise here)."""
    try:
        await wait_until_a_query_waits_for_a_lock(db)
    finally:
        go.set()
    return await asyncio.wait_for(asyncio.gather(*tasks), 15)


async def admin_and_dave(db, client_factory):
    """The admin, dave and dave's client, signed in."""
    admin = await create_user(db, "admin@example.com", is_admin=True)
    dave = await create_user(db, EMAIL)
    return admin, dave, await signed_in(client_factory, EMAIL)


def disabling(spawn, engine, admin, dave) -> asyncio.Task:
    async def disable():
        async with AsyncSession(engine, expire_on_commit=False) as other:
            await admin_service.set_status(other, admin, dave.id, UserStatus.DISABLED)

    return spawn(disable())


async def status_of(db, dave) -> UserStatus:
    query = select(User.status).where(User.id == dave.id)
    return await db.scalar(query.execution_options(populate_existing=True))


async def has_password(db, dave, password: str) -> bool:
    query = select(User.password_hash).where(User.id == dave.id)
    return verify_password(
        await db.scalar(query.execution_options(populate_existing=True)), password
    )


async def sessions_left(db) -> list[int]:
    return list(await db.scalars(select(UserSession.id)))


async def test_a_change_started_during_a_disable_is_kept_and_signs_this_device_out(
    client_factory, db, _db_engine, monkeypatch, spawn
):
    admin, dave, a = await admin_and_dave(db, client_factory)
    reached, go = pause(monkeypatch, admin_service, "revoke_all_sessions")  # after its first writes
    disable = disabling(spawn, _db_engine, admin, dave)
    await asyncio.wait_for(reached.wait(), 5)

    change = spawn(a.post("/account/password", data=CHANGE))
    r, _ = await run_blocked_by(db, go, change, disable)

    assert r.status_code == 303 and r.headers["location"] == "/login"  # its session was revoked
    assert await status_of(db, dave) == UserStatus.DISABLED
    assert await has_password(db, dave, NEW_PASSWORD)
    assert await sessions_left(db) == []


async def test_a_disable_started_during_a_change_waits_for_it(
    client_factory, db, _db_engine, monkeypatch, spawn
):
    admin, dave, a = await admin_and_dave(db, client_factory)
    reached, go = pause(monkeypatch, account_routes, "commit_and_publish", before=True)
    change = spawn(a.post("/account/password", data=CHANGE))
    await asyncio.wait_for(reached.wait(), 5)

    r, _ = await run_blocked_by(db, go, change, disabling(spawn, _db_engine, admin, dave))

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert await status_of(db, dave) == UserStatus.DISABLED
    assert await sessions_left(db) == []  # the disable came last and took this device's too


async def test_a_change_started_during_a_delete_is_refused(
    client_factory, db, _db_engine, monkeypatch, spawn
):
    admin, dave, a = await admin_and_dave(db, client_factory)
    reached, go = pause(monkeypatch, admin_service, "revoke_all_sessions")  # after its first writes

    async def delete():
        async with AsyncSession(_db_engine, expire_on_commit=False) as other:
            await admin_service.delete_user(other, admin, dave.id)

    deleting = spawn(delete())
    await asyncio.wait_for(reached.wait(), 5)

    change = spawn(a.post("/account/password", data=CHANGE))
    r, _ = await run_blocked_by(db, go, change, deleting)

    assert r.status_code == 400 and "Current password is incorrect." in r.text
    assert await db.scalar(select(func.count()).select_from(User).where(User.id == dave.id)) == 0


async def test_a_disable_started_during_a_reset_waits_for_it(
    client_factory, db, _db_engine, monkeypatch, spawn
):
    admin, dave, _ = await admin_and_dave(db, client_factory)
    token = await create_reset_token(db, dave)
    # Before the reset's last writes: the account's row is locked by now, the token row is used.
    reached, go = pause(monkeypatch, reset, "invalidate_reset_tokens", before=True)

    async def do_reset():
        async with AsyncSession(_db_engine, expire_on_commit=False) as other:
            await reset.reset_password(other, token, NEW_PASSWORD)

    resetting = spawn(do_reset())
    await asyncio.wait_for(reached.wait(), 5)

    await run_blocked_by(db, go, resetting, disabling(spawn, _db_engine, admin, dave))

    assert await status_of(db, dave) == UserStatus.DISABLED
    assert await has_password(db, dave, NEW_PASSWORD)


async def test_a_reset_link_request_during_a_reset_waits_for_it(
    db, _db_engine, monkeypatch, spawn, settings
):
    # A request ends every unused link and adds one: with two unused links it writes several
    # token rows, as the reset does, and the two would wait for each other's rows.
    dave = await create_user(db, EMAIL)
    await create_reset_token(db, dave)  # an older link, still unused
    token = await create_reset_token(db, dave)
    reached, go = pause(monkeypatch, reset, "invalidate_reset_tokens", before=True)
    mailed: list[str] = []

    class Mailer:
        async def send(self, to: str, subject: str, body: str) -> None:
            mailed.append(to)

    async def do_reset():
        async with AsyncSession(_db_engine, expire_on_commit=False) as other:
            await reset.reset_password(other, token, NEW_PASSWORD)

    async def do_request():
        async with AsyncSession(_db_engine, expire_on_commit=False) as other:
            await reset.request_password_reset(other, EMAIL, Mailer(), settings)

    resetting = spawn(do_reset())
    await asyncio.wait_for(reached.wait(), 5)

    await run_blocked_by(db, go, resetting, spawn(do_request()))

    assert mailed == [EMAIL]  # the request came last: its link is the one left
    unused = select(func.count()).select_from(PasswordResetToken)
    assert await db.scalar(unused.where(PasswordResetToken.used_at.is_(None))) == 1


@pytest.mark.parametrize("already", [True, False], ids=["already-disabled", "active"])
async def test_a_disable_locks_the_users_row_before_it_ends_sessions_and_links(db, already):
    admin = await create_user(db, "admin@example.com", is_admin=True)
    status = UserStatus.DISABLED if already else UserStatus.ACTIVE
    dave = await create_user(db, EMAIL, status=status)

    with count_queries(db) as statements:
        await admin_service.set_status(db, admin, dave.id, UserStatus.DISABLED)

    def first(*starts: str, containing: str = "\0") -> int:
        found = (i for i, s in enumerate(statements) if s.startswith(starts) or containing in s)
        return next(found, len(statements))  # past the end: there is none

    lock = first("UPDATE users", containing="FOR NO KEY UPDATE")
    assert lock < first("DELETE FROM user_sessions"), statements
    # Re-disabling flushes nothing, so it locks with a SELECT; a disable that changes the status
    # locks with that UPDATE and adds no statement.
    assert any("FOR NO KEY UPDATE" in s for s in statements) is already, statements


async def test_signing_out_the_other_devices_during_a_change_waits_for_it(
    client_factory, db, monkeypatch, spawn
):
    await create_user(db, EMAIL)
    b = await signed_in(client_factory, EMAIL)
    await signed_in(client_factory, EMAIL)  # C
    a = await signed_in(client_factory, EMAIL)  # the last row: B's delete reaches it after C's
    a_id = await db.scalar(
        select(UserSession.id).where(
            UserSession.token_hash == hash_token(a.cookies[SESSION_COOKIE])
        )
    )
    # After A's rotation, before its delete of the others: A holds its own row, B would take C's.
    reached, go = pause(monkeypatch, account_routes, "revoke_all_sessions", before=True)
    change = spawn(a.post("/account/password", data=CHANGE))
    await asyncio.wait_for(reached.wait(), 5)

    revoke = spawn(b.post("/account/sessions/revoke-others"))
    r_change, r_revoke = await run_blocked_by(db, go, change, revoke)

    # B's session went with A's change, so B has nothing to sign the others out of: it is told so,
    # and A's new session, which B's delete would have taken, stays.
    assert r_change.status_code == 303 and r_change.headers["location"] == "/account"
    assert r_revoke.status_code == 303 and r_revoke.headers["location"] == "/login"
    assert SESSION_COOKIE not in b.cookies
    assert FLASH_COOKIE not in r_revoke.headers.get("set-cookie", "")
    assert await sessions_left(db) == [a_id]
    assert (await a.get("/account")).status_code == 200
