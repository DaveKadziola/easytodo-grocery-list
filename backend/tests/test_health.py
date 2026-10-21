async def test_healthz_reports_db_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


async def test_healthz_reports_degraded_when_db_is_unreachable(client_factory):
    c = await client_factory(database_url="postgresql+asyncpg://easytodo:easytodo@127.0.0.1:1/x")
    r = await c.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "error"}
