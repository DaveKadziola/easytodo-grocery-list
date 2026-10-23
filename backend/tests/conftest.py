import asyncio
import atexit
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.types import ASGIApp
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import easytodo.models  # noqa: F401  (registers every table on Base.metadata)
from easytodo.config import Settings, get_settings
from easytodo.db import Base
from easytodo.mail.mailer import SentMail
from easytodo.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://easytodo:easytodo@localhost:54329/easytodo_test"
)
BASE_URL = "http://testserver"

ClientFactory = Callable[..., Awaitable[httpx.AsyncClient]]


def alembic_cfg(url: str = TEST_DATABASE_URL) -> Config:
    """Alembic config for the test database.

    Run commands with it in a worker thread (``await asyncio.to_thread(command.upgrade, cfg,
    "head")``): the async env.py calls ``asyncio.run``, which cannot nest in the test loop.
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["url"] = url
    # Keep pytest's logging setup; alembic.ini's fileConfig would disable existing loggers.
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
async def _migrated_db() -> None:
    await asyncio.to_thread(command.upgrade, alembic_cfg(), "head")


@pytest.fixture(scope="session")
async def _db_engine(_migrated_db: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def truncate_all_tables(engine: AsyncEngine) -> None:
    tables = Base.metadata.sorted_tables
    if not tables:
        return
    async with engine.begin() as conn:
        names = ", ".join(conn.dialect.identifier_preparer.format_table(t) for t in tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def _clean_db(_db_engine: AsyncEngine) -> AsyncIterator[None]:
    """Empties every table after the test (set up before, torn down after app and db)."""
    yield
    await truncate_all_tables(_db_engine)


@pytest.fixture(autouse=True)
def _no_gc_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts an app, and freezing the heap at startup costs a full collection
    (about 55 ms): only ``tests/test_lifespan.py`` runs the real ``_freeze_startup_heap``."""
    monkeypatch.setattr("easytodo.main._freeze_startup_heap", lambda: None)


# Where the files of attachments go unless a test says otherwise: one directory for the session,
# removed when it ends, so no test writes into the working directory.
SESSION_ATTACHMENTS_DIR = Path(tempfile.mkdtemp(prefix="easytodo-attachments-"))
atexit.register(shutil.rmtree, SESSION_ATTACHMENTS_DIR, ignore_errors=True)


def _test_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "secret_key": "x" * 40,
        "public_url": BASE_URL,
        "env": "test",
        "attachments_dir": SESSION_ATTACHMENTS_DIR,
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture
def settings() -> Settings:
    return _test_settings()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[None]:
    """The environment the CLI reads its settings from, pointing at the test database."""
    monkeypatch.setenv("EASYTODO_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("EASYTODO_SECRET_KEY", settings.secret_key.get_secret_value())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(settings: Settings, _clean_db: None) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def _running_app(app: FastAPI) -> AsyncIterator[ASGIApp]:
    """The app with its lifespan entered once, shared by ``client`` and ``client_factory()``."""
    async with LifespanManager(app) as manager:
        yield manager.app


def _new_client(asgi_app: ASGIApp, origin: str = BASE_URL) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url=origin,
        headers={"Origin": origin},
    )


@pytest.fixture
def mail_outbox(app: FastAPI, _running_app: ASGIApp) -> list[SentMail]:
    """The mails ``app`` has sent so far, oldest first (the test settings use the console
    backend, which keeps them)."""
    return app.state.mailer.outbox


@pytest.fixture
async def client(_running_app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with _new_client(_running_app) as c:
        yield c


@pytest.fixture
async def client_factory(_running_app: ASGIApp, settings: Settings) -> AsyncIterator[ClientFactory]:
    """``await client_factory()`` -> a new client (own cookies) for the same app;
    ``await client_factory(**overrides)`` -> a client for a separate app built from ``settings``
    with those fields overridden (its base URL and Origin follow the overridden public origin).
    """
    async with AsyncExitStack() as stack:

        async def factory(**settings_overrides: object) -> httpx.AsyncClient:
            if not settings_overrides:
                return await stack.enter_async_context(_new_client(_running_app))
            other = _test_settings(**(settings.model_dump() | settings_overrides))
            manager = await stack.enter_async_context(LifespanManager(create_app(other)))
            return await stack.enter_async_context(_new_client(manager.app, other.public_origin))

        yield factory


@pytest.fixture
async def https_client(_clean_db: None) -> AsyncIterator[httpx.AsyncClient]:
    """A client (base URL and ``Origin`` https://testserver) of an app whose public URL is
    https://testserver, served as the ``proxy`` compose profile runs it behind Caddy: uvicorn's
    proxy-headers middleware trusting every peer (``EASYTODO_FORWARDED_ALLOW_IPS=*``), so
    ``X-Forwarded-Proto`` / ``X-Forwarded-For`` set the request's scheme and client."""
    https = _test_settings(public_url="https://testserver")
    async with LifespanManager(create_app(https)) as manager:
        proxied = ProxyHeadersMiddleware(manager.app, trusted_hosts="*")
        async with _new_client(proxied, https.public_origin) as c:
            yield c


@pytest.fixture
async def db(_db_engine: AsyncEngine, _clean_db: None) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(_db_engine, expire_on_commit=False) as session:
        yield session
