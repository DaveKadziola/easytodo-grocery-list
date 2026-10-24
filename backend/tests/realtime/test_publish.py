"""Publishing and listening: every event is a pg_notify in the transaction of its change (so it
leaves only with the commit), carries the request's tab as its origin and fits in a
notification; the listener hands events on and asks for a resync after losing its connection;
signing out other devices publishes the sessions it revoked."""

import asyncio
import json

import asyncpg
import pytest
from factories import create_board, create_category, create_user
from helpers import login, signed_in
from sqlalchemy import select, text

from easytodo.models import UserSession
from easytodo.realtime import events
from easytodo.realtime import listener as listener_module
from easytodo.realtime.events import (
    MAX_PAYLOAD_BYTES,
    BoardEvent,
    EventType,
    commit_and_publish,
)
from easytodo.realtime.listener import LISTENER_APPLICATION_NAME, EventListener, listener_dsn

HX = {"HX-Request": "true"}


async def next_payload(probe: asyncio.Queue[str], timeout: float = 2.0) -> dict:
    return json.loads(await asyncio.wait_for(probe.get(), timeout))


async def assert_nothing_published(probe: asyncio.Queue[str], within: float = 1.0) -> None:
    with pytest.raises(TimeoutError):
        payload = await asyncio.wait_for(probe.get(), within)
        pytest.fail(f"published {payload}")


async def test_notify_is_sent_only_after_commit(db, listener_probe, monkeypatch):
    event = BoardEvent(EventType.BOARD_UPDATED, board_id=1)
    commit = db.commit

    async def failing_commit():
        await db.rollback()
        raise RuntimeError("the commit failed")

    monkeypatch.setattr(db, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="the commit failed"):
        await commit_and_publish(db, event)
    await assert_nothing_published(listener_probe)  # rolled back with the change

    async def commit_after_a_look():
        await assert_nothing_published(listener_probe, within=0.3)  # still in the transaction
        await commit()

    monkeypatch.setattr(db, "commit", commit_after_a_look)
    await commit_and_publish(db, event)
    assert await next_payload(listener_probe) == {
        "type": "board.updated",
        "board_id": 1,
        "category_ids": [],
        "item_ids": [],
        "user_id": None,
        "session_ids": [],
        "origin": None,
    }


async def test_published_event_carries_the_requests_client_id(client, db, listener_probe):
    owner = await create_user(db, "dave@example.com")
    board = await create_board(db, owner)
    dairy = await create_category(db, board, "Dairy")
    await login(client, "dave@example.com")

    r = await client.post(
        f"/categories/{dairy.id}/items",
        data={"name": "Milk"},
        headers=HX | {"X-Client-Id": "tab-A"},
    )

    assert r.status_code == 200
    assert await next_payload(listener_probe) == {
        "type": "category.changed",
        "board_id": board.id,
        "category_ids": [dairy.id],
        "item_ids": [],
        "user_id": None,
        "session_ids": [],
        "origin": "tab-A",
    }


async def test_large_event_is_split_into_payloads_under_8000_bytes(db, listener_probe):
    session_ids = tuple(range(10**12, 10**12 + 1000))  # about 14 kB of ids

    await commit_and_publish(
        db, BoardEvent(EventType.SESSIONS_REVOKED, user_id=7, session_ids=session_ids)
    )

    received: list[int] = []
    while len(received) < len(session_ids):
        raw = await asyncio.wait_for(listener_probe.get(), 2)
        assert len(raw.encode()) < MAX_PAYLOAD_BYTES == 8000
        payload = json.loads(raw)
        assert (payload["type"], payload["user_id"]) == ("sessions.revoked", 7)
        received += payload["session_ids"]
    assert received == list(session_ids)
    await assert_nothing_published(listener_probe, within=0.3)


async def test_large_item_id_list_is_split_too(db, listener_probe):
    item_ids = tuple(range(10**12, 10**12 + 1000))  # about 14 kB of ids

    await commit_and_publish(
        db, BoardEvent(EventType.CATEGORY_CHANGED, board_id=3, category_ids=(7,), item_ids=item_ids)
    )

    received: list[int] = []
    while len(received) < len(item_ids):
        raw = await asyncio.wait_for(listener_probe.get(), 2)
        assert len(raw.encode()) < MAX_PAYLOAD_BYTES
        payload = json.loads(raw)
        assert (payload["type"], payload["board_id"]) == ("category.changed", 3)
        assert payload["category_ids"] == [7]  # goes with every part
        received += payload["item_ids"]
    assert received == list(item_ids)
    await assert_nothing_published(listener_probe, within=0.3)


async def test_events_go_out_on_the_notify_channel(db, settings, monkeypatch):
    monkeypatch.setattr(events, "NOTIFY_CHANNEL", "board_events_probe")
    connection = await asyncpg.connect(listener_dsn(settings.database_url.get_secret_value()))
    payloads: asyncio.Queue[str] = asyncio.Queue()
    try:
        await connection.add_listener(
            "board_events_probe",
            lambda _connection, _pid, _channel, payload: payloads.put_nowait(payload),
        )

        await commit_and_publish(db, BoardEvent(EventType.BOARD_UPDATED, board_id=1))

        assert (await next_payload(payloads))["type"] == "board.updated"
    finally:
        await connection.close()


async def test_listener_reconnect_triggers_resync(db, settings):
    received: asyncio.Queue[BoardEvent] = asyncio.Queue()
    reconnected = asyncio.Event()

    async def on_event(event: BoardEvent) -> None:
        received.put_nowait(event)

    async def on_reconnect() -> None:
        reconnected.set()

    listener = EventListener(
        listener_dsn(settings.database_url.get_secret_value()), on_event, on_reconnect
    )
    await listener.start()
    try:
        event = BoardEvent(EventType.CATEGORY_CHANGED, board_id=3, category_ids=(4,))
        await commit_and_publish(db, event)
        assert await asyncio.wait_for(received.get(), 2) == event

        terminated = await db.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                " WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": LISTENER_APPLICATION_NAME},
        )
        await db.commit()
        assert terminated == 1
        assert not reconnected.is_set()
        await asyncio.wait_for(reconnected.wait(), 5)  # events may have been missed meanwhile

        await commit_and_publish(db, event)
        assert await asyncio.wait_for(received.get(), 2) == event  # listening again
    finally:
        await listener.stop()


async def test_listener_notices_a_connection_that_no_longer_answers(db, settings, monkeypatch):
    """Half-open: the connection looks fine, but nothing comes back (a NAT or a firewall dropped
    it without a word). A periodic SELECT 1 finds out, and the listener reconnects."""
    monkeypatch.setattr(listener_module, "HEALTH_CHECK_INTERVAL_S", 0.2)
    monkeypatch.setattr(listener_module, "HEALTH_CHECK_TIMEOUT_S", 0.2)
    received: asyncio.Queue[BoardEvent] = asyncio.Queue()
    reconnected = asyncio.Event()

    async def on_event(event: BoardEvent) -> None:
        received.put_nowait(event)

    async def on_reconnect() -> None:
        reconnected.set()

    listener = EventListener(
        listener_dsn(settings.database_url.get_secret_value()), on_event, on_reconnect
    )
    await listener.start()
    try:

        async def no_answer(*_args: object) -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(listener, "_ping", no_answer)
        await asyncio.wait_for(reconnected.wait(), 5)

        event = BoardEvent(EventType.CATEGORY_CHANGED, board_id=3, category_ids=(4,))
        await commit_and_publish(db, event)
        assert await asyncio.wait_for(received.get(), 2) == event  # listening again
    finally:
        await listener.stop()


async def test_listener_keeps_going_after_an_unexpected_error(db, settings, caplog):
    """A failure in the listener's loop is logged and the loop goes on; it never ends silently,
    leaving the worker without events for good."""
    received: asyncio.Queue[BoardEvent] = asyncio.Queue()
    failures = []

    async def on_event(event: BoardEvent) -> None:
        received.put_nowait(event)

    async def on_reconnect() -> None:
        if not failures:
            failures.append(True)
            raise RuntimeError("boom")

    listener = EventListener(
        listener_dsn(settings.database_url.get_secret_value()), on_event, on_reconnect
    )
    await listener.start()
    try:
        await db.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": LISTENER_APPLICATION_NAME},
        )
        await db.commit()
        event = BoardEvent(EventType.CATEGORY_CHANGED, board_id=3, category_ids=(4,))
        for _ in range(100):  # until it listens again, after the failed on_reconnect
            await commit_and_publish(db, event)
            try:
                assert await asyncio.wait_for(received.get(), 0.1) == event
                break
            except TimeoutError:
                continue
        else:
            pytest.fail("the listener never listened again")
        assert failures and "boom" in caplog.text
    finally:
        await listener.stop()


async def test_listener_start_survives_an_unreachable_database():
    async def ignore(*_args: object) -> None:
        pass

    listener = EventListener("postgresql://easytodo:easytodo@127.0.0.1:1/x", ignore, ignore)

    await listener.start()  # keeps retrying in the background instead of failing the app
    await listener.stop()


def test_listener_dsn_is_the_database_url_without_the_driver():
    assert (
        listener_dsn("postgresql+asyncpg://easytodo:p%40ss@db:5432/easytodo")
        == "postgresql://easytodo:p%40ss@db:5432/easytodo"
    )


async def test_signing_out_other_devices_publishes_their_sessions(
    client_factory, db, listener_probe
):
    user = await create_user(db, "dave@example.com")
    await signed_in(client_factory, "dave@example.com")  # the phone
    laptop = await signed_in(client_factory, "dave@example.com")
    phone_session, _ = await db.scalars(select(UserSession.id).order_by(UserSession.id))

    r = await laptop.post("/account/sessions/revoke-others")

    assert r.status_code == 303
    assert await next_payload(listener_probe) == {
        "type": "sessions.revoked",
        "board_id": None,
        "category_ids": [],
        "item_ids": [],
        "user_id": user.id,
        "session_ids": [phone_session],
        "origin": None,
    }

    r = await laptop.post("/account/sessions/revoke-others")  # nobody else is signed in now

    assert r.status_code == 303
    await assert_nothing_published(listener_probe)
