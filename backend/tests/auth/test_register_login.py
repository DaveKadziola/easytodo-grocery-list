from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argon2 import PasswordHasher
from factories import DEFAULT_PASSWORD, create_user
from helpers import login
from sqlalchemy import func, select, update

from easytodo.auth import service
from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.models import User, UserSession, UserStatus
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.security.passwords import needs_rehash, verify_password
from easytodo.security.tokens import hash_token

WRONG_PASSWORD = "not-the-right-password"


async def post_form(client: httpx.AsyncClient, url: str, **fields: str) -> httpx.Response:
    """POST a form the way the browser does: with the CSRF cookie's value in ``csrf_token``."""
    if CSRF_COOKIE not in client.cookies:
        await client.get("/login")
    return await client.post(url, data=fields | {"csrf_token": client.cookies[CSRF_COOKIE]})


async def count_rows(db, model) -> int:
    return await db.scalar(select(func.count()).select_from(model))


async def test_register_logs_in_and_normalizes_email(client, db):
    await client.get("/register")
    r = await client.post(
        "/register",
        data={
            "email": " Dave@Example.COM ",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies["easytodo_csrf"],
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert (await db.scalar(select(User.email))) == "dave@example.com"
    cookie = r.headers["set-cookie"]
    assert "easytodo_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    r = await client.get("/boards")
    assert r.status_code == 200
    assert "dave@example.com" in r.text  # the header shows who is signed in


async def test_email_is_normalized_everywhere(client, db):  # Review Focus 5
    await create_user(db, "dave@example.com")
    await login(client, "  DAVE@example.com")
    assert "dave@example.com" in (await client.get("/boards")).text

    r = await post_form(client, "/register", email=" Dave@Example.com ", password=DEFAULT_PASSWORD)
    assert r.status_code == 400
    assert "An account with this email already exists." in r.text
    assert await count_rows(db, User) == 1


async def test_register_rejects_short_password(client, db):
    r = await post_form(client, "/register", email="dave@example.com", password="short")
    assert r.status_code == 400
    assert "Password must be at least 10 characters." in r.text
    assert 'value="dave@example.com"' in r.text  # the form keeps what was typed
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert await count_rows(db, User) == 0


async def test_register_rejects_invalid_email(client, db):
    r = await post_form(client, "/register", email="dave at example", password=DEFAULT_PASSWORD)
    assert r.status_code == 400
    assert "Enter a valid email address." in r.text
    assert await count_rows(db, User) == 0


async def test_register_disabled_returns_404(client_factory):
    c = await client_factory(registration_open=False)
    assert (await c.get("/register")).status_code == 404
    r = await post_form(c, "/register", email="dave@example.com", password=DEFAULT_PASSWORD)
    assert r.status_code == 404
    assert 'href="/register"' not in (await c.get("/login")).text


async def test_login_page_links_to_registration_when_open(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert 'href="/register"' in r.text


async def test_wrong_password_and_unknown_email_same_message(client, db):
    await create_user(db, "dave@example.com")
    wrong = await post_form(client, "/login", email="dave@example.com", password=WRONG_PASSWORD)
    unknown = await post_form(client, "/login", email="eve@example.com", password=WRONG_PASSWORD)
    for r in (wrong, unknown):
        assert r.status_code == 400
        assert "Invalid email or password." in r.text
        assert "set-cookie" not in r.headers
    # Apart from the echoed address the two pages are identical: nothing tells them apart.
    assert wrong.text.replace("dave@", "X@") == unknown.text.replace("eve@", "X@")


@pytest.mark.parametrize("email", ["eve@example.com", "not an email"])
async def test_unknown_email_still_verifies_a_password(client, monkeypatch, email):
    verified: list[str] = []

    def spy(hash: str, pw: str) -> bool:
        verified.append(hash)
        return verify_password(hash, pw)

    monkeypatch.setattr(service, "verify_password", spy)
    r = await post_form(client, "/login", email=email, password=DEFAULT_PASSWORD)
    assert r.status_code == 400
    assert "Invalid email or password." in r.text
    assert verified == [service.DUMMY_HASH]  # same argon2 work as for a known address


async def test_overlong_password_is_refused_before_hashing(client, db, monkeypatch):
    await create_user(db, "dave@example.com")
    verified: list[str] = []
    monkeypatch.setattr(service, "verify_password", lambda hash, pw: verified.append(hash))
    r = await post_form(client, "/login", email="dave@example.com", password="x" * 1000)
    assert r.status_code == 400
    assert "Invalid email or password." in r.text
    assert verified == []


async def test_lockout_after_10_failures(client, db):
    user = await create_user(db, "dave@example.com")
    for attempt in range(1, 11):
        r = await post_form(client, "/login", email="dave@example.com", password=WRONG_PASSWORD)
        assert r.status_code == 400
        expected = (
            "Too many failed attempts. Try again in 15 minutes."
            if attempt == 10
            else "Invalid email or password."
        )
        assert expected in r.text, attempt
    r = await post_form(client, "/login", email="dave@example.com", password=DEFAULT_PASSWORD)
    assert r.status_code == 400
    assert "Too many failed attempts. Try again in 15 minutes." in r.text
    assert await count_rows(db, UserSession) == 0

    await db.refresh(user)
    lock_left = user.locked_until - datetime.now(UTC)
    assert timedelta(minutes=14) < lock_left <= timedelta(minutes=15)

    # Once the 15 minutes are over, the right password works again.
    await db.execute(update(User).values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    await login(client, "dave@example.com")


async def test_successful_login_resets_failures_and_rehashes(client, db):
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    user = await create_user(db, "dave@example.com")
    user.password_hash = weak_hash
    await db.commit()
    for _ in range(9):
        await post_form(client, "/login", email="dave@example.com", password=WRONG_PASSWORD)
    await db.refresh(user)
    assert user.failed_login_count == 9

    await login(client, "dave@example.com")
    await db.refresh(user)
    assert user.failed_login_count == 0
    assert datetime.now(UTC) - user.last_login_at < timedelta(minutes=1)
    assert user.password_hash != weak_hash
    assert not needs_rehash(user.password_hash)
    assert verify_password(user.password_hash, DEFAULT_PASSWORD)


async def test_disabled_account_cannot_login(client, db):
    await create_user(db, "dave@example.com", status=UserStatus.DISABLED)
    r = await post_form(client, "/login", email="dave@example.com", password=DEFAULT_PASSWORD)
    assert r.status_code == 400
    assert "This account is disabled." in r.text
    # Without the right password a disabled account looks like any other failure.
    r = await post_form(client, "/login", email="dave@example.com", password=WRONG_PASSWORD)
    assert r.status_code == 400
    assert "Invalid email or password." in r.text
    assert await count_rows(db, UserSession) == 0


async def test_disabled_users_session_stops_working(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    await db.execute(update(User).values(status=UserStatus.DISABLED))
    await db.commit()
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F"


async def test_login_rotates_session_token(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    first = client.cookies[SESSION_COOKIE]
    await login(client, "dave@example.com")
    second = client.cookies[SESSION_COOKIE]
    assert first != second
    hashes = set(await db.scalars(select(UserSession.token_hash)))
    assert hashes == {hash_token(first), hash_token(second)}


async def test_session_cookie_and_row(client, db, settings):
    user = await create_user(db, "dave@example.com")
    client.headers["User-Agent"] = "TestBrowser/1.0"
    r = await post_form(client, "/login", email="dave@example.com", password=DEFAULT_PASSWORD)
    raw_token = client.cookies[SESSION_COOKIE]
    max_age = settings.session_ttl_days * 24 * 3600
    assert r.headers["set-cookie"] == (
        f"{SESSION_COOKIE}={raw_token}; HttpOnly; Max-Age={max_age}; Path=/; SameSite=lax"
    )
    session = await db.scalar(select(UserSession))
    # The database holds only the token's hash: a leaked table yields no usable cookie.
    assert session.token_hash == hash_token(raw_token) != raw_token
    assert session.user_id == user.id and session.mfa_pending is False
    assert (session.user_agent, session.ip) == ("TestBrowser/1.0", "127.0.0.1")
    ttl = session.expires_at - datetime.now(UTC)
    full_ttl = timedelta(days=settings.session_ttl_days)
    assert full_ttl - timedelta(minutes=1) < ttl <= full_ttl


async def test_long_user_agent_is_truncated(client, db):
    await create_user(db, "dave@example.com")
    client.headers["User-Agent"] = "Mozilla/5.0 " + "x" * 300
    await login(client, "dave@example.com")
    user_agent = await db.scalar(select(UserSession.user_agent))
    assert len(user_agent) == 255
    assert user_agent == client.headers["User-Agent"][:255]


async def test_logout_revokes_session(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    token = client.cookies[SESSION_COOKIE]
    r = await client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert SESSION_COOKIE not in client.cookies  # the cookie was cleared
    client.cookies.set(SESSION_COOKIE, token)
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F"
    assert await count_rows(db, UserSession) == 0


async def test_anonymous_request_redirects_to_login(client):
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F"


async def test_anonymous_htmx_request_gets_hx_redirect(client):
    r = await client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize(
    "url,params",
    [
        ("/login", {"next": "https://evil.example"}),
        ("/login", {"next": "//evil.example"}),
        ("/login", {"next": "/\\evil.example"}),
        ("/login?next=/%5Cevil.example", None),  # the query parser decodes %5C to a backslash
        ("/login", {"next": "/\t/evil.example"}),  # browsers drop the tab: //evil.example
        ("/login", {"next": "/\x7f/evil.example"}),
        ("/login", {"next": "evil.example"}),
        ("/login", {"next": ""}),
    ],
)
async def test_next_param_rejects_external_urls(client, db, url, params):
    await create_user(db, "dave@example.com")
    await client.get("/login")
    r = await client.post(
        url,
        params=params,
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_next_param_keeps_local_paths(client, db):
    await create_user(db, "dave@example.com")
    await client.get("/login")
    r = await client.post(
        "/login",
        params={"next": "/account?tab=sessions"},
        data={
            "email": "dave@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/account?tab=sessions"


async def test_expired_session_is_rejected(client, db):
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    await db.execute(
        update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    r = await client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F"


async def test_login_rate_limited_per_ip(client):
    for _ in range(20):
        r = await post_form(client, "/login", email="eve@example.com", password=WRONG_PASSWORD)
        assert r.status_code == 400
    r = await post_form(client, "/login", email="eve@example.com", password=WRONG_PASSWORD)
    assert r.status_code == 429


async def test_register_rate_limited_per_ip(client):
    for _ in range(10):
        r = await post_form(client, "/register", email="eve@example.com", password="short")
        assert r.status_code == 400
    r = await post_form(client, "/register", email="eve@example.com", password="short")
    assert r.status_code == 429


@pytest.mark.parametrize(
    ("path", "email"),
    [("/login", "dave@example.com"), ("/register", "eve@example.com"), ("/logout", "")],
)
async def test_auth_posts_without_csrf_token_are_403(client, db, path, email):
    # The auth forms are checked by the global CSRF dependency, ahead of their own rate limits
    # and of any account work (the guard for moving that work after the check).
    await create_user(db, "dave@example.com")
    await login(client, "dave@example.com")
    del client.headers[CSRF_HEADER]
    users, sessions = await count_rows(db, User), await count_rows(db, UserSession)

    r = await client.post(path, data={"email": email, "password": DEFAULT_PASSWORD})

    assert r.status_code == 403
    assert "Invalid CSRF token." in r.text
    assert (await count_rows(db, User), await count_rows(db, UserSession)) == (users, sessions)
    assert (await client.get("/boards")).status_code == 200  # Dave is still signed in
