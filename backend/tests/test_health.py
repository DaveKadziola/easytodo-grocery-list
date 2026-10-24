import asyncio
from typing import Self


class _HangingEngine:
    """An engine whose database never answers: ``connect()`` succeeds, entering the connection
    context waits for good (a blackholed host, which asyncpg gives 60 s to connect)."""

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def test_healthz_reports_db_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


async def test_healthz_reports_degraded_when_db_is_unreachable(client_factory):
    c = await client_factory(database_url="postgresql+asyncpg://easytodo:easytodo@127.0.0.1:1/x")
    r = await c.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "error"}


async def test_healthz_answers_503_in_time_when_the_db_hangs(app, client, monkeypatch):
    monkeypatch.setattr(app.state, "engine", _HangingEngine())
    monkeypatch.setattr("easytodo.main.HEALTHZ_TIMEOUT_S", 0.2)
    started = asyncio.get_running_loop().time()
    r = await asyncio.wait_for(client.get("/healthz"), 5)
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "error"}
    assert asyncio.get_running_loop().time() - started < 1
