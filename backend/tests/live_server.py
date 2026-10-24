"""The app served by a real Uvicorn on a free local port, for browser and WebSocket tests.

Synchronous on purpose: Playwright's sync API leaves its own event loop marked as running in the
test thread, so everything here that needs asyncio (migrations, emptying the tables) runs on a
fresh loop in a worker thread. Import the fixture into the conftest of each test package that
uses it (``from live_server import live_server``); ``serving`` starts further servers on the same
database (several workers).
"""

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any

import pytest
import uvicorn
from alembic import command
from conftest import alembic_cfg, truncate_all_tables
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from easytodo.config import Settings
from easytodo.main import create_app

STARTUP_TIMEOUT_S = 10


@dataclass(frozen=True)
class LiveServer:
    url: str  # http://127.0.0.1:<port>, also the app's public_url
    settings: Settings
    app: FastAPI  # running in the server's thread (and event loop)


def in_worker_thread[T](fn: Callable[..., T], *args: Any) -> T:
    """``fn(*args)`` in a new thread, where no event loop runs yet (use ``asyncio.run`` as
    ``fn`` for a coroutine)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


async def _empty_tables(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        await truncate_all_tables(engine)
    finally:
        await engine.dispose()


@cache
def _migrate_test_db() -> None:
    """Once per test run (the async ``_migrated_db`` fixture cannot run in a sync test)."""
    in_worker_thread(command.upgrade, alembic_cfg(), "head")  # env.py calls asyncio.run


@contextmanager
def serving(settings: Settings) -> Iterator[LiveServer]:
    """``create_app(settings)`` served by ``uvicorn.Server`` in a thread on a free port, with
    ``public_url`` set to that address."""
    _migrate_test_db()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    live_settings = Settings(_env_file=None, **(settings.model_dump() | {"public_url": url}))
    app = create_app(live_settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"the live server at {url} did not start")
            time.sleep(0.01)
        yield LiveServer(url, live_settings, app)
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)
        sock.close()
    assert not thread.is_alive(), "the live server did not stop"


@contextmanager
def emptying_tables(settings: Settings) -> Iterator[None]:
    """Every table of the test database is emptied on the way out."""
    try:
        yield
    finally:
        in_worker_thread(asyncio.run, _empty_tables(settings.database_url.get_secret_value()))


@pytest.fixture
def live_server(settings: Settings) -> Iterator[LiveServer]:
    """``serving(settings)`` for the test; every table is emptied after it."""
    with emptying_tables(settings), serving(settings) as server:
        yield server
