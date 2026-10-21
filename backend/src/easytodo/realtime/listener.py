"""Each worker's ``LISTEN board_events``: one dedicated asyncpg connection (not from the pool)
that hands every notified event to the hub.

A lost connection is re-established with a backoff of 1 s doubling up to 30 s; once it is back,
``on_reconnect`` runs, because events notified meanwhile were missed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg
from sqlalchemy.engine import make_url

from easytodo.realtime.events import NOTIFY_CHANNEL, BoardEvent

log = logging.getLogger(__name__)

LISTENER_APPLICATION_NAME = "easytodo-listener"  # how the connection shows in pg_stat_activity
MIN_BACKOFF_S = 1
MAX_BACKOFF_S = 30
CONNECT_TIMEOUT_S = 10
CLOSE_TIMEOUT_S = 5
_CONNECTION_ERRORS = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)


def listener_dsn(database_url: str) -> str:
    """The asyncpg DSN for ``database_url`` (``EASYTODO_DATABASE_URL``, a SQLAlchemy URL)."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


class EventListener:
    def __init__(
        self,
        dsn: str,
        on_event: Callable[[BoardEvent], Awaitable[None]],
        on_reconnect: Callable[[], Awaitable[None]],
    ) -> None:
        self._dsn = dsn
        self._on_event = on_event
        self._on_reconnect = on_reconnect
        self._connection: asyncpg.Connection | None = None
        self._lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Listen from now on. The first connection is made before this returns; if the
        database cannot be reached, it is retried in the background instead."""
        connected = await self._connect()
        self._task = asyncio.create_task(self._keep_listening(connected))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        connection, self._connection = self._connection, None
        if connection is not None and not connection.is_closed():
            try:
                await connection.close(timeout=CLOSE_TIMEOUT_S)
            except _CONNECTION_ERRORS:
                connection.terminate()

    async def _connect(self) -> bool:
        connection = None
        try:
            connection = await asyncpg.connect(
                self._dsn,
                timeout=CONNECT_TIMEOUT_S,
                server_settings={"application_name": LISTENER_APPLICATION_NAME},
            )
            self._lost.clear()
            connection.add_termination_listener(lambda _connection: self._lost.set())
            await connection.add_listener(NOTIFY_CHANNEL, self._notified)
        except _CONNECTION_ERRORS as exc:
            log.warning("Cannot listen for board events (%s); retrying.", exc)
            if connection is not None:
                connection.terminate()
            return False
        self._connection = connection
        return True

    async def _keep_listening(self, connected: bool) -> None:
        delay = MIN_BACKOFF_S
        while True:
            if connected:
                delay = MIN_BACKOFF_S
                await self._lost.wait()
                self._connection = None
                log.warning("Lost the board events connection; reconnecting.")
            await asyncio.sleep(delay)
            connected = await self._connect()
            if connected:
                await self._on_reconnect()
            else:
                delay = min(delay * 2, MAX_BACKOFF_S)

    async def _notified(
        self, _connection: asyncpg.Connection, _pid: int, _channel: str, payload: str
    ) -> None:
        try:
            event = BoardEvent.from_json(payload)
        except (ValueError, KeyError, TypeError):
            log.warning("Ignoring a malformed board event: %.200r", payload)
            return
        await self._on_event(event)
