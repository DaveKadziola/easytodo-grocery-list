import json
from urllib.parse import parse_qs, urlsplit

import pytest
from factories import create_user
from fastapi import FastAPI
from helpers import login
from starlette.requests import Request

from easytodo.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotAuthenticated,
    NotFound,
    ValidationFailed,
)
from easytodo.web import is_htmx, render

HX = {"HX-Request": "true"}


@pytest.mark.parametrize(
    "cls,status",
    [
        (NotFound, 404),
        (Forbidden, 403),
        (Conflict, 409),
        (ValidationFailed, 422),
        (NotAuthenticated, 401),
    ],
)
def test_error_classes_carry_status_and_message(cls, status):
    exc = cls("Some message.")
    assert isinstance(exc, AppError)
    assert (exc.status_code, exc.message) == (status, "Some message.")


def test_not_found_has_a_default_message():
    assert NotFound().message == "Not found."


async def test_htmx_not_found_triggers_toast_and_resync(err_app_client):
    r = await err_app_client.get("/raise-404", headers=HX)
    assert r.status_code == 404 and r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": "Not found."},
        "resync": True,
    }
    assert r.content == b""


@pytest.mark.parametrize(
    "status,message",
    [
        (403, "You don't have permission to do that."),
        (409, 'A category named <b>O\'Reilly "x"</b> already exists.'),
        (422, "Board name must be 1–80 characters."),  # non-latin-1: must survive the header
    ],
)
async def test_htmx_errors_trigger_toast_without_resync(err_app_client, status, message):
    r = await err_app_client.get(f"/raise-{status}", headers=HX)
    assert r.status_code == status and r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {"toast": {"level": "error", "message": message}}
    assert r.content == b""


async def test_htmx_not_authenticated_redirects_to_login(err_app_client):
    r = await err_app_client.get("/raise-401", headers=HX)
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"
    assert "hx-trigger" not in r.headers


async def test_not_authenticated_redirects_to_login_with_next(err_app_client):
    r = await err_app_client.get("/raise-401")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fraise-401"

    r = await err_app_client.get("/raise-401", params={"tab": "open", "q": "a&b c"})
    assert r.status_code == 303
    location = r.headers["location"]
    assert location == "/login?next=%2Fraise-401%3Ftab%3Dopen%26q%3Da%2526b%2Bc"
    assert parse_qs(urlsplit(location).query) == {"next": ["/raise-401?tab=open&q=a%26b+c"]}


async def test_other_errors_render_the_error_page(err_app_client):
    r = await err_app_client.get("/raise-403")
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/html")
    assert "403" in r.text
    assert "You don&#39;t have permission to do that." in r.text


async def test_error_page_escapes_the_message(err_app_client):  # Review Focus 1
    r = await err_app_client.get("/raise-409")
    assert r.status_code == 409
    assert "<b>" not in r.text
    assert "A category named &lt;b&gt;O&#39;Reilly &#34;x&#34;&lt;/b&gt; already exists." in r.text


async def test_request_validation_error_is_mapped_like_validation_failed(err_app_client):
    r = await err_app_client.get("/validate", params={"n": "abc"}, headers=HX)
    assert r.status_code == 422 and r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {
            "level": "error",
            "message": "Input should be a valid integer, unable to parse string as an integer",
        }
    }

    r = await err_app_client.get("/validate")
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("text/html")
    assert "Field required" in r.text


def _request(app: FastAPI, state: dict, headers: tuple[tuple[bytes, bytes], ...] = ()) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": list(headers),
        "app": app,
        "state": state,
    }
    return Request(scope)


def test_render_adds_csrf_token_current_user_and_settings(mini_app, settings):
    user = object()
    request = _request(mini_app, {"csrf_token": "tok", "user": user})
    r = render(request, "errors/error.html", {"status_code": 418, "message": "Teapot."}, 418)
    assert r.status_code == 418
    assert r.context["csrf_token"] == "tok"
    assert r.context["current_user"] is user
    assert r.context["settings"] is settings
    assert r.context["message"] == "Teapot."


def test_render_defaults_to_no_user_and_lets_the_context_win(mini_app):
    request = _request(mini_app, {})
    r = render(request, "errors/error.html", {"status_code": 400, "message": "x"})
    assert r.status_code == 200
    assert r.context["csrf_token"] is None and r.context["current_user"] is None
    r = render(
        request, "errors/error.html", {"status_code": 400, "message": "x", "current_user": 1}
    )
    assert r.context["current_user"] == 1


def test_is_htmx(mini_app):
    assert is_htmx(_request(mini_app, {}, ((b"hx-request", b"true"),))) is True
    assert is_htmx(_request(mini_app, {}, ((b"hx-request", b"false"),))) is False
    assert is_htmx(_request(mini_app, {})) is False


async def test_unknown_page_renders_the_error_page_not_json(client):
    """A mistyped URL or an old v1 bookmark gets a page (or, for htmx, a toast), never FastAPI's
    {"detail": "Not Found"}."""
    for path in ("/nope", "/v1/get_categories", "/apidocs", "/static/nope.js"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("text/html"), path
        assert "Page not found." in r.text

    r = await client.get("/nope", headers=HX)
    assert r.status_code == 404 and r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": "Page not found."}  # no resync: not board data
    }


async def test_wrong_method_renders_the_error_page_not_json(client):
    r = await client.put("/login")
    assert r.status_code == 405
    assert r.headers["content-type"].startswith("text/html")
    assert "allow" in r.headers
    assert "This page does not accept that request." in r.text


async def test_bad_path_parameter_is_a_404_not_pydantic_text(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    for path in ("/boards/abc", "/categories/1.5", "/boards/abc/members"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert "Input should be" not in r.text
        assert "Not found." in r.text


async def test_htmx_not_authenticated_comes_back_to_the_page_after_sign_in(err_app_client):
    """The session ended on an open board: signing in again leads back to that board."""
    current = {"HX-Current-URL": "http://testserver/boards/7?x=1"}
    r = await err_app_client.get("/raise-401", headers=HX | current)
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login?next=%2Fboards%2F7%3Fx%3D1"

    for elsewhere in ("https://evil.example/boards/7", "not a url", "http://testserver//evil"):
        r = await err_app_client.get("/raise-401", headers=HX | {"HX-Current-URL": elsewhere})
        assert r.headers["hx-redirect"] == "/login", elsewhere


async def test_htmx_sign_in_again_comes_back_to_a_page_at_an_extra_origin(
    extra_origin_app_client, extra_origin
):
    current = {"HX-Current-URL": f"{extra_origin}/boards/7"}
    r = await extra_origin_app_client.get("/raise-401", headers=HX | current)
    assert r.headers["hx-redirect"] == "/login?next=%2Fboards%2F7"

    foreign = {"HX-Current-URL": "http://192.168.1.100:8000/boards/7"}
    r = await extra_origin_app_client.get("/raise-401", headers=HX | foreign)
    assert r.headers["hx-redirect"] == "/login"
