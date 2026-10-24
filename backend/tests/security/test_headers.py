import httpx
import pytest
from factories import create_user
from helpers import signed_in, toast_message
from pydantic import AnyHttpUrl

from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.errors import SERVER_ERROR
from easytodo.main import create_app

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


async def test_security_headers_present(client):
    r = await client.get("/healthz")
    assert r.headers["content-security-policy"].startswith("default-src 'self'; script-src 'self';")
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # http w testach
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


async def test_security_headers_on_error_responses(client):
    r = await client.get("/no-such-page")
    assert r.status_code == 404
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


async def test_csrf_cookie_flags_over_http(client):
    r = await client.get("/healthz")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("easytodo_csrf=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie


async def test_https_public_url_adds_hsts_and_secure_csrf_cookie(client_factory):
    c = await client_factory(public_url="https://testserver")
    r = await c.get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert "Secure" in r.headers["set-cookie"]
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_are_disabled(client, path):  # Swagger UI needs a CDN, which the CSP blocks
    assert (await client.get(path)).status_code == 404


def app_that_fails(settings):
    """An app (https, so with HSTS) whose ``/boom`` fails with an error nobody handles, and a
    client for it that gets the 500 the server sends rather than the exception."""
    app = create_app(settings.model_copy(update={"public_url": AnyHttpUrl("https://testserver")}))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="https://testserver")


async def test_unhandled_error_gets_the_error_page_and_the_security_headers(settings):
    async with app_that_fails(settings) as c:
        page = await c.get("/boom")
        hx = await c.get("/boom", headers={"HX-Request": "true"})

    for r in (page, hx):
        assert r.status_code == 500 and "secret detail" not in r.text
        assert {name: r.headers.get(name) for name in HEADERS} == HEADERS
        assert "strict-transport-security" in r.headers
    assert page.headers["content-type"].startswith("text/html") and SERVER_ERROR in page.text
    assert hx.headers["hx-reswap"] == "none" and toast_message(hx) == SERVER_ERROR


async def test_an_error_page_that_cannot_be_rendered_is_plain_text_with_the_headers(
    settings, monkeypatch
):
    """The template needs data that the failure may have made unreadable: then not even the page
    is shown, but the answer is still a 500 that names no detail, with the same headers."""

    def cannot_render(*args, **kwargs):
        raise RuntimeError("the page needs the database")

    monkeypatch.setattr("easytodo.errors.render", cannot_render)
    async with app_that_fails(settings) as c:
        r = await c.get("/boom")

    assert r.status_code == 500 and r.text == SERVER_ERROR
    assert r.headers["content-type"].startswith("text/plain")
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS
    assert "strict-transport-security" in r.headers


async def test_a_database_failure_still_gets_the_error_page(client_factory):
    """Looking up the session is what fails: the page is made without the user, so without the
    database."""
    c = await client_factory(database_url="postgresql+asyncpg://easytodo:easytodo@127.0.0.1:1/x")
    c._transport.raise_app_exceptions = False  # the server's 500, not the exception
    c.cookies.set(SESSION_COOKIE, "x" * 43)

    r = await c.get("/boards")

    assert r.status_code == 500 and SERVER_ERROR in r.text
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


async def test_the_error_page_of_a_signed_in_user_does_not_show_them(
    client_factory, db, monkeypatch
):
    """A route fails after ``CurrentUser`` has loaded the user: the 500 page is still made
    without them (the failure may be the database's), so it names nobody."""
    await create_user(db, "dave@example.com", display_name="Dave Kowalski")
    c = await signed_in(client_factory, "dave@example.com")
    c._transport.raise_app_exceptions = False  # the server's 500, not the exception

    async def fail(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("easytodo.boards.routes.list_boards", fail)

    r = await c.get("/boards")

    assert r.status_code == 500 and SERVER_ERROR in r.text
    assert "dave@example.com" not in r.text and "Dave Kowalski" not in r.text
    assert "secret detail" not in r.text
