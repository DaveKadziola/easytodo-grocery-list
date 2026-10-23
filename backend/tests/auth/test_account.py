import json
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest
from factories import DEFAULT_PASSWORD, create_user
from helpers import login
from sqlalchemy import select, update
from starlette.requests import Request

from easytodo.auth import account_routes
from easytodo.auth.deps import require_user
from easytodo.auth.sessions import SESSION_COOKIE, create_session
from easytodo.errors import Forbidden, PasswordChangeRequired
from easytodo.models import User, UserSession
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.security.passwords import verify_password
from easytodo.security.tokens import hash_token
from easytodo.web import FLASH_COOKIE

NEW_PASSWORD = "an-even-better-passphrase"
EMAIL = "dave@example.com"


async def sign_in(client: httpx.AsyncClient, email: str = EMAIL, user_agent: str | None = None):
    if user_agent:
        client.headers["User-Agent"] = user_agent
    await login(client, email)
    return client


def change_form(
    current: str = DEFAULT_PASSWORD, new: str = NEW_PASSWORD, confirm: str | None = None
) -> dict[str, str]:
    return {
        "current_password": current,
        "new_password": new,
        "new_password_confirm": new if confirm is None else confirm,
    }


async def session_id(db, client: httpx.AsyncClient) -> int:
    """The id of the session row behind ``client``'s cookie."""
    token_hash = hash_token(client.cookies[SESSION_COOKIE])
    return await db.scalar(select(UserSession.id).where(UserSession.token_hash == token_hash))


async def session_ids(db) -> set[int]:
    return set(await db.scalars(select(UserSession.id)))


async def password_hash(db) -> str:
    return await db.scalar(select(User.password_hash).where(User.email == EMAIL))


async def test_change_password_keeps_current_and_revokes_other_sessions(client_factory, db):
    await create_user(db, EMAIL)
    a, b = await client_factory(), await client_factory()
    await sign_in(a)
    await sign_in(b)
    assert len(await session_ids(db)) == 2

    r = await a.post("/account/password", data=change_form())

    assert r.status_code == 303
    assert r.headers["location"] == "/account"
    assert await session_ids(db) == {await session_id(db, a)}
    assert (await a.get("/account")).status_code == 200
    r = await b.get("/account")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert verify_password(await password_hash(db), NEW_PASSWORD)
    assert not verify_password(await password_hash(db), DEFAULT_PASSWORD)


async def test_old_password_stops_working_and_new_one_works(client_factory, db):
    await create_user(db, EMAIL)
    await sign_in(await client_factory())
    await (await sign_in(await client_factory())).post("/account/password", data=change_form())

    old = await client_factory()
    await old.get("/login")
    r = await old.post(
        "/login",
        data={"email": EMAIL, "password": DEFAULT_PASSWORD, "csrf_token": old.cookies[CSRF_COOKIE]},
    )
    assert r.status_code == 400 and "Invalid email or password." in r.text

    new = await client_factory()
    await login(new, EMAIL, NEW_PASSWORD)
    assert (await new.get("/account")).status_code == 200


async def test_successful_change_shows_a_toast_through_the_flash_cookie(client, db):
    await create_user(db, EMAIL)
    await sign_in(client)

    r = await client.post("/account/password", data=change_form())

    flash = json.loads(unquote(r.cookies[FLASH_COOKIE]))
    assert flash == {"level": "info", "message": "Password changed."}
    set_cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(FLASH_COOKIE))
    assert "Path=/" in set_cookie and "SameSite=lax" in set_cookie
    assert "HttpOnly" not in set_cookie  # app.js has to read it


async def test_change_password_requires_current_password(client_factory, db):
    await create_user(db, EMAIL)
    a, b = await client_factory(), await client_factory()
    await sign_in(a)
    await sign_in(b)
    before = await password_hash(db)

    r = await a.post("/account/password", data=change_form(current="not-the-right-password"))

    assert r.status_code == 400
    assert "Current password is incorrect." in r.text
    assert FLASH_COOKIE not in r.headers.get("set-cookie", "")
    assert await password_hash(db) == before
    assert len(await session_ids(db)) == 2
    assert (await b.get("/account")).status_code == 200


@pytest.mark.parametrize(
    ("form", "message"),
    [
        (change_form(confirm="something-else-entirely"), "Passwords do not match."),
        (change_form(new="short", confirm="short"), "Password must be at least 10 characters."),
        (change_form(new="x" * 257), "Password must be at most 256 characters."),
        ({}, "Current password is incorrect."),
    ],
    ids=["mismatch", "too-short", "too-long", "empty-form"],
)
async def test_change_password_rejects_invalid_input(client, db, form, message):
    await create_user(db, EMAIL, must_change_password=True)
    await sign_in(client)
    before = await password_hash(db)

    r = await client.post("/account/password", data=form)

    assert r.status_code == 400
    assert message in r.text
    assert await password_hash(db) == before
    assert await db.scalar(select(User.must_change_password)) is True


async def test_overlong_current_password_is_refused_before_hashing(client, db, monkeypatch):
    await create_user(db, EMAIL)
    await sign_in(client)
    calls = []

    async def spy(*args) -> bool:
        calls.append(args)
        return False

    monkeypatch.setattr(account_routes, "verify_password_async", spy)

    r = await client.post("/account/password", data=change_form(current="x" * 1000))

    assert r.status_code == 400
    assert "Current password is incorrect." in r.text
    assert calls == []


async def test_password_form_requires_sign_in(client):
    r = await client.get("/account/password")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Faccount%2Fpassword"


async def test_password_guesses_are_counted_per_account(client_factory, client_from_ip, db):
    await create_user(db, EMAIL)
    await create_user(db, "bob@example.com")
    dave = await sign_in(await client_factory())
    bob = await sign_in(await client_factory(), "bob@example.com")  # the same address as Dave
    wrong = change_form(current="not-the-right-password")

    statuses = [(await dave.post("/account/password", data=wrong)).status_code for _ in range(11)]
    assert statuses == [400] * 10 + [429]
    # Bob shares Dave's address, not his budget.
    assert (await bob.post("/account/password", data=wrong)).status_code == 400
    # Dave's budget goes with his session, whichever address it is used from.
    elsewhere = await client_from_ip("203.0.113.7")
    elsewhere.cookies.update(dave.cookies)
    elsewhere.headers[CSRF_HEADER] = dave.headers[CSRF_HEADER]
    assert (await elsewhere.post("/account/password", data=wrong)).status_code == 429


async def test_must_change_password_redirects_everything(client, db):
    await create_user(db, "temp@example.com", must_change_password=True)
    await login(client, "temp@example.com")

    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/account/password"
    r = await client.get("/account")
    assert r.status_code == 303 and r.headers["location"] == "/account/password"
    r = await client.get("/boards")
    assert r.status_code == 303 and r.headers["location"] == "/account/password"

    page = await client.get("/account/password")  # the one page that stays reachable
    assert page.status_code == 200
    assert "You must change your password before you continue." in page.text


async def test_must_change_password_blocks_other_methods_with_403(client, db):
    await create_user(db, "temp@example.com", must_change_password=True)
    await login(client, "temp@example.com")

    r = await client.post("/account/sessions/revoke-others")
    assert r.status_code == 403
    assert "You must change your password first." in r.text

    r = await client.post("/account/sessions/revoke-others", headers={"HX-Request": "true"})
    assert r.status_code == 403
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": "You must change your password first."}
    }


async def test_must_change_password_htmx_get_gets_hx_redirect(client, db):
    await create_user(db, "temp@example.com", must_change_password=True)
    await login(client, "temp@example.com")

    r = await client.get("/", headers={"HX-Request": "true"})

    assert r.headers["hx-redirect"] == "/account/password"
    assert "location" not in r.headers  # the browser navigates, htmx does not swap a page in


@pytest.mark.parametrize(
    ("method", "path", "outcome"),
    [
        ("GET", "/account/password", "allowed"),
        ("POST", "/account/password", "allowed"),
        ("POST", "/logout", "allowed"),  # no route calls require_user there yet
        ("GET", "/account/password/extra", "redirect"),
        ("GET", "/", "redirect"),
        ("POST", "/boards", "forbidden"),
        ("DELETE", "/account/sessions/1", "forbidden"),
    ],
)
async def test_require_user_lets_a_forced_change_use_only_the_password_form_and_logout(
    db, settings, method, path, outcome
):
    user = await create_user(db, EMAIL, must_change_password=True)
    token, _ = await create_session(
        db, user, mfa_pending=False, user_agent=None, ip=None, settings=settings
    )
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
        }
    )
    if outcome == "allowed":
        assert await require_user(request, db) is user
    elif outcome == "redirect":
        with pytest.raises(PasswordChangeRequired) as redirect:
            await require_user(request, db)
        assert redirect.value.location == "/account/password"
    else:
        with pytest.raises(Forbidden, match="You must change your password first."):
            await require_user(request, db)


async def test_must_change_password_still_allows_sign_out(client, db):
    await create_user(db, "temp@example.com", must_change_password=True)
    await login(client, "temp@example.com")

    r = await client.post("/logout")

    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert await session_ids(db) == set()


async def test_must_change_password_flag_cleared_after_change(client, db):
    await create_user(db, EMAIL, must_change_password=True)
    await sign_in(client)
    assert (await client.get("/")).headers["location"] == "/account/password"

    r = await client.post("/account/password", data=change_form())

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert await db.scalar(select(User.must_change_password)) is False
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/boards"
    assert (await client.get("/account")).status_code == 200


async def test_only_a_forced_change_refuses_the_current_password(client, db):
    # The temporary-password check (Task 15) applies to forced changes only.
    await create_user(db, EMAIL)
    await sign_in(client)

    r = await client.post("/account/password", data=change_form(new=DEFAULT_PASSWORD))

    assert r.status_code == 303 and r.headers["location"] == "/account"


async def test_change_password_form_is_plain_when_not_forced(client, db):
    await create_user(db, EMAIL)
    await sign_in(client)
    r = await client.get("/account/password")
    assert r.status_code == 200
    assert "You must change your password" not in r.text
    for field in ("current_password", "new_password", "new_password_confirm"):
        assert f'name="{field}"' in r.text


async def test_account_page_lists_sessions_and_marks_this_device(client_factory, db):
    user = await create_user(db, EMAIL)
    laptop = await sign_in(await client_factory(), user_agent="Firefox on Laptop")
    phone = await sign_in(await client_factory(), user_agent="Chrome on Phone")
    phone_id = await session_id(db, phone)
    await db.execute(
        update(UserSession)
        .where(UserSession.id == phone_id)
        .values(last_seen_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC), ip="203.0.113.7")
    )
    await db.commit()

    r = await laptop.get("/account")

    assert r.status_code == 200
    assert user.email in r.text
    assert "Firefox on Laptop" in r.text and "Chrome on Phone" in r.text
    assert "203.0.113.7" in r.text and "2026-01-02 03:04 UTC" in r.text
    assert r.text.count("This device") == 1
    # Only the other session can be revoked from the list.
    assert f'action="/account/sessions/{phone_id}/revoke"' in r.text
    assert f'action="/account/sessions/{await session_id(db, laptop)}/revoke"' not in r.text
    assert 'action="/account/sessions/revoke-others"' in r.text


async def test_account_page_without_other_sessions_has_no_revoke_controls(client, db):
    await create_user(db, EMAIL)
    await sign_in(client)
    r = await client.get("/account")
    assert r.text.count("This device") == 1
    assert "/revoke" not in r.text


async def test_account_page_escapes_user_agent(client, db):
    await create_user(db, EMAIL)
    await sign_in(client, user_agent="""<script>alert(1)</script> O'Reilly "x" <b>""")
    r = await client.get("/account")
    assert "<script>alert(1)" not in r.text and "<b>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


async def test_account_page_ignores_expired_sessions_and_other_users(client_factory, db):
    await create_user(db, EMAIL)
    await create_user(db, "eve@example.com")
    mine = await sign_in(await client_factory())
    await sign_in(await client_factory(), "eve@example.com", user_agent="Eves Secret Browser")
    old = await sign_in(await client_factory(), user_agent="Expired Browser")
    await db.execute(
        update(UserSession)
        .where(UserSession.id == await session_id(db, old))
        .values(expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    await db.commit()

    r = await mine.get("/account")

    assert "Eves Secret Browser" not in r.text
    assert "Expired Browser" not in r.text


async def test_account_pages_require_sign_in(client):
    r = await client.get("/account")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Faccount"
    r = await client.post(
        "/account/sessions/revoke-others", data={"csrf_token": client.cookies[CSRF_COOKIE]}
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


async def test_header_links_to_the_account_page(client, db):
    await create_user(db, EMAIL)
    await sign_in(client)
    assert 'href="/account"' in (await client.get("/boards")).text


async def test_cannot_revoke_someone_elses_session(client_factory, db):
    await create_user(db, EMAIL)
    await create_user(db, "eve@example.com")
    dave = await sign_in(await client_factory())
    eve = await sign_in(await client_factory(), "eve@example.com")
    eves_id = await session_id(db, eve)
    before = await session_ids(db)

    r = await dave.post(f"/account/sessions/{eves_id}/revoke")

    assert r.status_code == 404
    assert await session_ids(db) == before
    assert (await eve.get("/account")).status_code == 200


@pytest.mark.parametrize("session_id_", ["999999", str(2**63), str(2**200), "0", "-1"])
async def test_revoking_an_unknown_session_is_404(client, db, session_id_):
    await create_user(db, EMAIL)
    await sign_in(client)
    r = await client.post(f"/account/sessions/{session_id_}/revoke")
    assert r.status_code == 404


async def test_revoke_one_of_my_sessions(client_factory, db):
    await create_user(db, EMAIL)
    a, b = await sign_in(await client_factory()), await sign_in(await client_factory())

    r = await a.post(f"/account/sessions/{await session_id(db, b)}/revoke")

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert await session_ids(db) == {await session_id(db, a)}
    assert (await b.get("/account")).status_code == 303


async def test_revoke_others_leaves_only_current(client_factory, db):
    await create_user(db, EMAIL)
    await create_user(db, "eve@example.com")
    a, b, c = [await sign_in(await client_factory()) for _ in range(3)]
    eve = await sign_in(await client_factory(), "eve@example.com")

    r = await a.post("/account/sessions/revoke-others")

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert await session_ids(db) == {await session_id(db, a), await session_id(db, eve)}
    assert (await a.get("/account")).status_code == 200
    assert (await b.get("/account")).status_code == 303
    assert (await c.get("/account")).status_code == 303
    assert (await eve.get("/account")).status_code == 200
