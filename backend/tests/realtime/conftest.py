"""Real-time fixtures: a probe on the board events channel, live servers (one, or two on the same
database as two workers) and devices signed in to them."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack

import asyncpg
import httpx
import pytest
from helpers import login
from live_server import LiveServer, emptying_tables, live_server, serving  # noqa: F401 (fixture)

from easytodo.config import Settings
from easytodo.realtime.events import NOTIFY_CHANNEL
from easytodo.realtime.hub import Subscriber
from easytodo.realtime.listener import listener_dsn

type SignIn = Callable[[LiveServer, str], Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def listener_probe(settings: Settings) -> AsyncIterator[asyncio.Queue[str]]:
    """Every payload notified on the board events channel from now on, in order."""
    connection = await asyncpg.connect(listener_dsn(settings.database_url))
    payloads: asyncio.Queue[str] = asyncio.Queue()
    await connection.add_listener(
        NOTIFY_CHANNEL, lambda _connection, _pid, _channel, payload: payloads.put_nowait(payload)
    )
    try:
        yield payloads
    finally:
        await connection.close()


@pytest.fixture
def live_server_two_workers(settings: Settings) -> Iterator[tuple[LiveServer, LiveServer]]:
    """Two apps, as two Uvicorn workers run them, on the same database (each on its own port)."""
    with emptying_tables(settings), serving(settings) as first, serving(settings) as second:
        yield first, second


@pytest.fixture
async def sign_in() -> AsyncIterator[SignIn]:
    """``await sign_in(server, email)`` -> a new HTTP client of that live server, signed in as
    ``email`` (its own cookies, so its own session: another device)."""
    async with AsyncExitStack() as stack:

        async def device(server: LiveServer, email: str) -> httpx.AsyncClient:
            client = httpx.AsyncClient(base_url=server.url, headers={"Origin": server.url})
            await stack.enter_async_context(client)
            await login(client, email)
            return client

        yield device


@pytest.fixture
def subscribed(live_server: LiveServer, monkeypatch) -> list[Subscriber]:  # noqa: F811
    """Every socket the live server's hub subscribes from now on."""
    hub = live_server.app.state.hub
    subscribe = hub.subscribe
    calls: list[Subscriber] = []

    def spy(subscriber: Subscriber) -> None:
        calls.append(subscriber)
        subscribe(subscriber)

    monkeypatch.setattr(hub, "subscribe", spy)
    return calls
