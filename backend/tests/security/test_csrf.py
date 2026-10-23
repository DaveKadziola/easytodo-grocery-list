import json

import httpx
from fastapi import FastAPI
from starlette.datastructures import FormData

from easytodo.main import create_app
from easytodo.security.csrf import CSRF_COOKIE, CSRF_FIELD, CSRF_HEADER, verify_csrf
from easytodo.security.ratelimit import RateLimiter


async def _token(c: httpx.AsyncClient) -> str:
    await c.get("/token")
    return c.cookies[CSRF_COOKIE]


def test_csrf_names():
    assert (CSRF_COOKIE, CSRF_HEADER, CSRF_FIELD) == ("easytodo_csrf", "X-CSRF-Token", "csrf_token")


async def test_csrf_cookie_is_issued_once_and_exposed_on_request_state(csrf_app_client):
    r = await csrf_app_client.get("/token")
    token = r.text
    assert len(token) == 43 and csrf_app_client.cookies[CSRF_COOKIE] == token
    assert r.headers["set-cookie"] == f"easytodo_csrf={token}; HttpOnly; Path=/; SameSite=lax"
    r = await csrf_app_client.get("/token")
    assert r.text == token  # the existing cookie is reused, not rotated
    assert "set-cookie" not in r.headers


async def test_csrf_cookie_is_secure_with_https_public_url(https_csrf_app_client):
    r = await https_csrf_app_client.get("/token")
    assert r.headers["set-cookie"].endswith("; Secure")


async def test_safe_methods_need_no_token(csrf_app_client):
    del csrf_app_client.headers["Origin"]
    assert (await csrf_app_client.get("/token")).status_code == 200  # no Origin, no cookie


async def test_post_without_csrf_header_is_403(csrf_app_client):
    await _token(csrf_app_client)
    r = await csrf_app_client.post("/submit")
    assert r.status_code == 403
    assert "Invalid CSRF token." in r.text


async def test_post_with_foreign_origin_is_403(csrf_app_client):
    token = await _token(csrf_app_client)
    r = await csrf_app_client.post(
        "/submit", headers={CSRF_HEADER: token, "Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert "Open the app at http://testserver." in r.text


async def test_post_from_an_extra_origin_passes(extra_origin_app_client, extra_origin):
    token = await _token(extra_origin_app_client)
    r = await extra_origin_app_client.post("/submit", headers={CSRF_HEADER: token})
    assert r.status_code == 200
    r = await extra_origin_app_client.post(
        "/submit", headers={CSRF_HEADER: token, "Origin": "http://testserver"}
    )
    assert r.status_code == 200  # the public origin still works
    del extra_origin_app_client.headers["Origin"]  # without it, the Referer counts the same way
    r = await extra_origin_app_client.post(
        "/submit", headers={CSRF_HEADER: token, "Referer": f"{extra_origin}/boards/1"}
    )
    assert r.status_code == 200


async def test_post_from_neither_public_nor_extra_origin_is_403(extra_origin_app_client):
    token = await _token(extra_origin_app_client)
    r = await extra_origin_app_client.post(
        "/submit", headers={CSRF_HEADER: token, "Origin": "http://192.168.1.100:8000"}
    )
    assert r.status_code == 403
    assert (
        "This page was opened at a different address. Open the app at http://testserver." in r.text
    )


async def test_post_with_matching_token_and_origin_passes(csrf_app_client):
    token = await _token(csrf_app_client)
    r = await csrf_app_client.post("/submit", headers={CSRF_HEADER: token})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_form_field_token_is_accepted(csrf_app_client):
    token = await _token(csrf_app_client)
    r = await csrf_app_client.post("/submit", data={CSRF_FIELD: token, "name": "Milk"})
    assert r.status_code == 200


async def test_multipart_form_field_token_is_accepted(csrf_app_client):
    token = await _token(csrf_app_client)
    # (None, value) parts carry no filename, so the body is multipart with text fields only.
    r = await csrf_app_client.post(
        "/submit", files={CSRF_FIELD: (None, token), "name": (None, "Milk")}
    )
    assert r.request.headers["content-type"].startswith("multipart/form-data")
    assert r.status_code == 200


async def test_wrong_token_is_403(csrf_app_client):
    await _token(csrf_app_client)
    assert (
        await csrf_app_client.post("/submit", headers={CSRF_HEADER: "x" * 43})
    ).status_code == 403
    assert (await csrf_app_client.post("/submit", data={CSRF_FIELD: "x" * 43})).status_code == 403


async def test_wrong_header_is_not_rescued_by_form_field(csrf_app_client):
    token = await _token(csrf_app_client)
    r = await csrf_app_client.post(
        "/submit", headers={CSRF_HEADER: "x" * 43}, data={CSRF_FIELD: token}
    )
    assert r.status_code == 403


async def test_token_without_cookie_is_403(csrf_app_client):
    r = await csrf_app_client.post("/submit", headers={CSRF_HEADER: "x" * 43})
    assert r.status_code == 403


async def test_non_ascii_token_is_403_not_500(csrf_app_client):
    await _token(csrf_app_client)
    r = await csrf_app_client.post("/submit", data={CSRF_FIELD: "zażółć gęślą jaźń"})
    assert r.status_code == 403


async def test_referer_is_checked_when_origin_is_missing(csrf_app_client):
    token = await _token(csrf_app_client)
    del csrf_app_client.headers["Origin"]
    headers = {CSRF_HEADER: token}

    r = await csrf_app_client.post(
        "/submit", headers=headers | {"Referer": "http://testserver/boards/1?x=1"}
    )
    assert r.status_code == 200
    for referer in (
        "https://evil.example/boards/1",
        "http://testserver.evil.example/",
        "http://[::1",
    ):
        r = await csrf_app_client.post("/submit", headers=headers | {"Referer": referer})
        assert r.status_code == 403, referer
    assert (await csrf_app_client.post("/submit", headers=headers)).status_code == 403


async def test_foreign_origin_is_not_rescued_by_referer(csrf_app_client):
    token = await _token(csrf_app_client)
    r = await csrf_app_client.post(
        "/submit",
        headers={
            CSRF_HEADER: token,
            "Origin": "https://evil.example",
            "Referer": "http://testserver/boards/1",
        },
    )
    assert r.status_code == 403


async def test_origin_scheme_must_match_public_url(https_csrf_app_client):
    token = await _token(https_csrf_app_client)
    r = await https_csrf_app_client.post(
        "/submit", headers={CSRF_HEADER: token, "Origin": "http://testserver"}
    )
    assert r.status_code == 403
    r = await https_csrf_app_client.post("/submit", headers={CSRF_HEADER: token})
    assert r.status_code == 200


async def test_htmx_csrf_failure_shows_toast(csrf_app_client):
    await _token(csrf_app_client)
    r = await csrf_app_client.post("/submit", headers={"HX-Request": "true"})
    assert r.status_code == 403
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": "Invalid CSRF token."}
    }


async def test_global_csrf_dependency_does_not_break_websocket_routes(mini_app):
    # FastAPI applies global dependencies to WebSocket routes too; drive one over raw ASGI.
    incoming = [{"type": "websocket.connect"}]
    sent: list[dict] = []

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
        "root_path": "",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "subprotocols": [],
    }
    await mini_app(scope, receive, send)
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.send", "websocket.close"]
    assert sent[1]["text"] == "hello"


async def test_create_app_applies_csrf_to_every_route(settings):
    app: FastAPI = create_app(settings)
    assert isinstance(app.state.rate_limiter, RateLimiter)
    assert [d.dependency for d in app.router.dependencies] == [verify_csrf]

    @app.post("/probe")  # added to this instance only, after create_app
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        await c.get("/no-such-page")  # any response issues the cookie
        token = c.cookies[CSRF_COOKIE]
        r = await c.post("/probe", headers={CSRF_HEADER: token})  # no Origin/Referer
        assert r.status_code == 403 and "Open the app at http://testserver." in r.text
        r = await c.post("/probe", headers={CSRF_HEADER: token, "Origin": "http://testserver"})
        assert r.status_code == 200


TWO_MEGABYTES = b"x" * 2 * 1024 * 1024  # more than a form keeps in memory: it goes to a temp file


async def test_csrf_check_closes_the_form_it_read(csrf_app_client, mini_app):  # triage #16
    token = await _token(csrf_app_client)

    r = await csrf_app_client.post(  # a plain form post: the token is a field, not the header
        "/form-seen", data={CSRF_FIELD: token}, files={"file": ("big.bin", TWO_MEGABYTES)}
    )

    assert r.status_code == 200
    upload = mini_app.state.form_seen["file"]
    assert upload.size == len(TWO_MEGABYTES)
    assert upload.file.closed


async def test_csrf_check_closes_the_form_it_refused(csrf_app_client, monkeypatch):
    await _token(csrf_app_client)
    closed: list[FormData] = []
    close = FormData.close

    async def spying_close(self):
        closed.append(self)
        await close(self)

    monkeypatch.setattr(FormData, "close", spying_close)

    r = await csrf_app_client.post(
        "/form-seen", data={CSRF_FIELD: "forged"}, files={"file": ("big.bin", TWO_MEGABYTES)}
    )

    assert r.status_code == 403
    assert len(closed) == 1 and closed[0]["file"].file.closed


async def test_a_request_with_the_header_does_not_read_the_form(csrf_app_client, monkeypatch):
    token = await _token(csrf_app_client)
    closed: list[FormData] = []

    async def spying_close(self):
        closed.append(self)

    monkeypatch.setattr(FormData, "close", spying_close)

    r = await csrf_app_client.post("/submit", headers={CSRF_HEADER: token})

    assert r.status_code == 200
    assert closed == []
