"""Finding out, once at start-up, whether names can be sorted in Polish alphabetical order."""

import logging

from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from easytodo import db as database
from easytodo.db import NAME_COLLATION, detect_name_collation, name_collation
from easytodo.main import create_app


async def test_the_test_database_has_the_polish_collation(_db_engine):
    assert await detect_name_collation(_db_engine) == NAME_COLLATION, (
        "the test database must be UTF8, see README"
    )


async def test_a_collation_the_database_lacks_is_none_with_one_warning(
    _db_engine: AsyncEngine, monkeypatch, caplog
):
    monkeypatch.setattr(database, "NAME_COLLATION", "xx-no-such-x-icu")

    with caplog.at_level(logging.WARNING, logger="easytodo.db"):
        assert await detect_name_collation(_db_engine) is None

    [record] = [r for r in caplog.records if r.name == "easytodo.db"]
    assert "xx-no-such-x-icu" in record.getMessage() and "UTF8" in record.getMessage()


async def test_a_database_that_cannot_be_reached_is_none_too(caplog):
    engine = create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        with caplog.at_level(logging.WARNING, logger="easytodo.db"):
            assert await detect_name_collation(engine) is None
    finally:
        await engine.dispose()
    assert any(r.name == "easytodo.db" for r in caplog.records)


async def test_the_app_looks_for_the_collation_once_at_start_up(settings, _clean_db, monkeypatch):
    calls = []
    real = detect_name_collation

    async def counting(engine):
        calls.append(engine)
        return await real(engine)

    monkeypatch.setattr("easytodo.main.detect_name_collation", counting)
    monkeypatch.setattr(database, "_name_collation", None)  # as before the start (and put back)

    async with LifespanManager(create_app(settings)):
        assert name_collation() == NAME_COLLATION, "the test database must be UTF8, see README"

    assert len(calls) == 1
