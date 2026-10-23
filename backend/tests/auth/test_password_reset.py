"""Password reset by e-mail: the forgot-password form mails a single-use link that expires after
an hour; the link's form sets a new password and signs the account out everywhere."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pyotp
import pytest
from factories import DEFAULT_PASSWORD, create_board, create_user
from helpers import login
from markupsafe import escape
from sqlalchemy import func, select, update
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from easytodo.auth import reset
from easytodo.auth.reset import RESET_TTL
from easytodo.auth.sessions import SESSION_COOKIE
from easytodo.models import PasswordResetToken, RecoveryCode, User, UserSession, UserStatus
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.security.passwords import verify_password
from easytodo.security.tokens import hash_token
from easytodo.web import FLASH_COOKIE

EMAIL = "dave@example.com"
NEW_PASSWORD = "a-brand-new-passphrase"
SENT = "If an account exists for that email, we've sent a reset link."
INVALID_LINK = "This reset link is invalid or has expired."
UPDATED = "Password updated. Sign in with your new password."
RESET_LINK = re.compile(r"/password/reset/(\S+)")


async def with_csrf(client: httpx.AsyncClient) -> None:
    """Give ``client`` the CSRF cookie; its later requests carry the CSRF header."""
    if CSRF_COOKIE not in client.cookies:
        await client.get("/password/forgot")
    client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]


async def forgot(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    await with_csrf(client)
    return await client.post("/password/forgot", data={"email": email})


def token_in(mail) -> str:
    return RESET_LINK.search(mail.body)[1]


MAIL_WAIT_S = 2.0


async def request_link(client: httpx.AsyncClient, outbox, email: str = EMAIL) -> str:
    """Ask for a reset link for ``email``; returns the token from the mail it sends.

    The mail goes out after the response. The test transport returns only when the app is done
    with the request, background work included, but a server does not: wait (at most
    ``MAIL_WAIT_S``) until the outbox has grown."""
    sent = len(outbox)
    r = await forgot(client, email)
    assert r.status_code == 200, r.text
    deadline = asyncio.get_running_loop().time() + MAIL_WAIT_S
    while len(outbox) == sent and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(outbox) > sent, f"no reset mail within {MAIL_WAIT_S} s"
    return token_in(outbox[-1])


async def set_new_password(
    client: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD, confirm: str | None = None
) -> httpx.Response:
    await with_csrf(client)
    form = {
        "new_password": password,
        "new_password_confirm": password if confirm is None else confirm,
    }
    return await client.post(f"/password/reset/{token}", data=form)


async def try_login(client: httpx.AsyncClient, password: str) -> httpx.Response:
    await with_csrf(client)
    return await client.post("/login", data={"email": EMAIL, "password": password})


async def dave(db) -> User:
    query = select(User).where(User.email == EMAIL).execution_options(populate_existing=True)
    return await db.scalar(query)


async def test_reset_form_is_never_cached(client, db, mail_outbox):
    await create_user(db, EMAIL)
    token = await request_link(client, mail_outbox)

    assert (await client.get(f"/password/reset/{token}")).headers.get("cache-control") == "no-store"
    r = await set_new_password(client, token, "short")
    assert r.status_code == 400 and r.headers.get("cache-control") == "no-store"


async def test_unknown_email_gets_same_response_and_no_mail(client, mail_outbox):
    page = await client.get("/password/forgot")
    assert page.status_code == 200
    assert 'name="email"' in page.text

    r = await forgot(client, "nobody@example.com")

    assert r.status_code == 200
    assert escape(SENT) in r.text
    assert mail_outbox == []


async def test_known_unknown_and_invalid_emails_get_the_same_page(client, db, mail_outbox):
    await create_user(db, EMAIL)

    pages = [await forgot(client, email) for email in (EMAIL, "nobody@example.com", "not-an-email")]

    assert [r.status_code for r in pages] == [200, 200, 200]
    assert pages[0].text == pages[1].text == pages[2].text
    assert [mail.to for mail in mail_outbox] == [EMAIL]


async def test_disabled_account_gets_no_reset_mail(client, db, mail_outbox):
    await create_user(db, EMAIL, status=UserStatus.DISABLED)

    r = await forgot(client)

    assert r.status_code == 200 and escape(SENT) in r.text
    assert mail_outbox == []
    assert await db.scalar(select(func.count()).select_from(PasswordResetToken)) == 0


async def test_reset_request_normalizes_email(client, db, mail_outbox):  # Review Focus 5
    await create_user(db, EMAIL)

    await forgot(client, " Dave@Example.COM ")

    assert [mail.to for mail in mail_outbox] == [EMAIL]


async def test_reset_mail_has_the_link_and_only_its_hash_is_stored(client, db, mail_outbox):
    user = await create_user(db, EMAIL)

    r = await forgot(client)

    [mail] = mail_outbox
    token = token_in(mail)
    assert token not in r.text  # the link goes to the mailbox only
    assert mail.to == EMAIL
    assert mail.subject == "Reset your EasyToDo password"
    assert f"http://testserver/password/reset/{token}\n" in mail.body
    assert "expires in 60 minutes" in mail.body
    row = await db.scalar(select(PasswordResetToken))
    assert row.user_id == user.id
    assert row.token_hash == hash_token(token) != token
    assert row.used_at is None
    assert RESET_TTL == timedelta(minutes=60)
    left = row.expires_at - datetime.now(UTC)
    assert timedelta(minutes=59) < left <= timedelta(minutes=60)


async def test_reset_flow_changes_password_and_revokes_sessions(client_factory, db, mail_outbox):
    await create_user(db, EMAIL)
    old_device = await client_factory()
    await login(old_device, EMAIL)
    browser = await client_factory()
    token = await request_link(browser, mail_outbox)

    page = await browser.get(f"/password/reset/{token}")
    assert page.status_code == 200
    assert 'name="new_password"' in page.text and 'name="new_password_confirm"' in page.text
    r = await set_new_password(browser, token)

    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert json.loads(unquote(r.cookies[FLASH_COOKIE])) == {"level": "info", "message": UPDATED}
    assert await db.scalar(select(func.count()).select_from(UserSession)) == 0
    r = await old_device.get("/boards")  # the session from before the reset is signed out
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await try_login(browser, DEFAULT_PASSWORD)
    assert r.status_code == 400 and "Invalid email or password." in r.text
    await login(browser, EMAIL, NEW_PASSWORD)
    assert verify_password((await dave(db)).password_hash, NEW_PASSWORD)


async def test_reset_token_single_use_and_expires(client, db, mail_outbox):
    await create_user(db, EMAIL)
    token = await request_link(client, mail_outbox)
    assert (await set_new_password(client, token)).status_code == 303

    for r in (await client.get(f"/password/reset/{token}"), await set_new_password(client, token)):
        assert r.status_code == 404
        assert INVALID_LINK in r.text
    assert (await db.scalar(select(PasswordResetToken))).used_at is not None

    token = await request_link(client, mail_outbox)
    await db.execute(
        update(PasswordResetToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    for r in (await client.get(f"/password/reset/{token}"), await set_new_password(client, token)):
        assert r.status_code == 404
        assert INVALID_LINK in r.text
    assert verify_password((await dave(db)).password_hash, NEW_PASSWORD)  # only the first reset


async def test_unknown_reset_link_is_404(client):
    for r in (await client.get("/password/reset/bogus"), await set_new_password(client, "bogus")):
        assert r.status_code == 404
        assert INVALID_LINK in r.text


async def test_new_request_invalidates_previous_token(client, db, mail_outbox):
    await create_user(db, EMAIL)
    first = await request_link(client, mail_outbox)
    second = await request_link(client, mail_outbox)
    assert first != second

    r = await set_new_password(client, first)
    assert r.status_code == 404 and INVALID_LINK in r.text
    assert (await set_new_password(client, second)).status_code == 303


async def test_reset_form_rejects_bad_passwords_and_keeps_the_link(client, db, mail_outbox):
    await create_user(db, EMAIL)
    token = await request_link(client, mail_outbox)

    r = await set_new_password(client, token, "short")
    assert r.status_code == 400 and "Password must be at least 10 characters." in r.text
    r = await set_new_password(client, token, NEW_PASSWORD, confirm="something-else-entirely")
    assert r.status_code == 400 and "Passwords do not match." in r.text
    r = await set_new_password(client, token, "x" * 257)
    assert r.status_code == 400 and "Password must be at most 256 characters." in r.text
    assert verify_password((await dave(db)).password_hash, DEFAULT_PASSWORD)

    assert (await set_new_password(client, token)).status_code == 303  # still usable


async def test_reset_clears_lockout(client, db, mail_outbox):
    await create_user(db, EMAIL)
    await db.execute(
        update(User).values(
            failed_login_count=3, locked_until=datetime.now(UTC) + timedelta(minutes=10)
        )
    )
    await db.commit()
    r = await try_login(client, DEFAULT_PASSWORD)
    assert "Too many failed attempts. Try again in 15 minutes." in r.text

    await set_new_password(client, await request_link(client, mail_outbox))

    user = await dave(db)
    assert (user.failed_login_count, user.locked_until) == (0, None)
    await login(client, EMAIL, NEW_PASSWORD)


async def test_reset_clears_a_forced_password_change(client, db, mail_outbox):
    await create_user(db, EMAIL, must_change_password=True)

    await set_new_password(client, await request_link(client, mail_outbox))

    assert not (await dave(db)).must_change_password
    await login(client, EMAIL, NEW_PASSWORD)
    assert (await client.get("/boards")).status_code == 200  # not sent to /account/password


async def test_reset_keeps_2fa_required(client, db, user_with_2fa, mail_outbox):
    secret_enc = (await dave(db)).totp_secret_enc
    recovery_codes = set(await db.scalars(select(RecoveryCode.code_hash)))

    r = await set_new_password(client, await request_link(client, mail_outbox))
    assert r.status_code == 303

    r = await try_login(client, NEW_PASSWORD)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"
    user = await dave(db)
    assert user.totp_enabled and user.totp_secret_enc == secret_enc
    assert set(await db.scalars(select(RecoveryCode.code_hash))) == recovery_codes
    r = await client.post("/login/2fa", data={"code": pyotp.TOTP(user_with_2fa.secret).now()})
    assert r.status_code == 303 and r.headers["location"] == "/boards"


async def test_disabled_account_reset_link_is_404(client, db, mail_outbox):
    await create_user(db, EMAIL)
    token = await request_link(client, mail_outbox)
    await db.execute(update(User).values(status=UserStatus.DISABLED))
    await db.commit()

    r = await set_new_password(client, token)

    assert r.status_code == 404 and INVALID_LINK in r.text
    assert verify_password((await dave(db)).password_hash, DEFAULT_PASSWORD)


async def test_password_change_invalidates_outstanding_reset_link(client, db, mail_outbox):
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    token = await request_link(client, mail_outbox)
    form = {
        "current_password": DEFAULT_PASSWORD,
        "new_password": "changed-on-the-account-page",
        "new_password_confirm": "changed-on-the-account-page",
    }
    assert (await client.post("/account/password", data=form)).status_code == 303

    for r in (await client.get(f"/password/reset/{token}"), await set_new_password(client, token)):
        assert r.status_code == 404 and INVALID_LINK in r.text
    assert verify_password((await dave(db)).password_hash, "changed-on-the-account-page")


async def test_forgot_is_rate_limited(client):
    for _ in range(5):
        assert (await forgot(client, "nobody@example.com")).status_code == 200
    assert (await forgot(client, "nobody@example.com")).status_code == 429


async def test_forgot_answers_before_any_account_work(_running_app, app, db, monkeypatch):
    await create_user(db, EMAIL)
    order: list[str] = []
    held_connections: list[int] = []

    async def recording_app(scope, receive, send):
        async def record(message):
            if (  # the last part of a POST's response: the browser has its answer
                scope["method"] == "POST"
                and message["type"] == "http.response.body"
                and not message.get("more_body")
            ):
                order.append("response")
            await send(message)

        await _running_app(scope, receive, record)

    def new_token() -> str:
        order.append("token")
        return "token"

    async def send_mail(to, subject, body) -> bool:
        order.append("mail")
        held_connections.append(app.state.engine.pool.checkedout())
        return True

    monkeypatch.setattr(reset, "new_token", new_token)
    monkeypatch.setattr(app.state.mailer, "send", send_mail)
    transport = httpx.ASGITransport(app=recording_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers={"Origin": "http://testserver"}
    ) as client:
        known = await forgot(client, EMAIL)
        unknown = await forgot(client, "nobody@example.com")

    # The token and the mail come after the answer, and only for the account that exists, so
    # the time the answer takes does not tell the two apart.
    assert order == ["response", "token", "mail", "response"]
    assert known.text == unknown.text
    assert held_connections == [0]  # the mail is sent with no database connection held


async def test_reset_mails_are_capped_per_account(client, db, mail_outbox):
    await create_user(db, EMAIL)
    await create_user(db, "bob@example.com")

    pages = [await forgot(client) for _ in range(4)]

    assert [r.status_code for r in pages] == [200] * 4
    assert len({r.text for r in pages}) == 1  # the capped request shows the same page
    assert [mail.to for mail in mail_outbox] == [EMAIL] * 3
    assert await db.scalar(select(func.count()).select_from(PasswordResetToken)) == 3
    # The fourth request ended no link: the one in the third mail is the one that works.
    assert (await client.get(f"/password/reset/{token_in(mail_outbox[2])}")).status_code == 200
    assert (await client.get(f"/password/reset/{token_in(mail_outbox[1])}")).status_code == 404
    await forgot(client, "bob@example.com")  # another account has a budget of its own
    assert [mail.to for mail in mail_outbox][-1] == "bob@example.com"


async def test_a_failing_reset_request_still_answers(client, db, monkeypatch, caplog):
    await create_user(db, EMAIL)

    def no_token() -> str:
        raise RuntimeError("no randomness")

    monkeypatch.setattr(reset, "new_token", no_token)
    with caplog.at_level(logging.ERROR):
        r = await forgot(client)

    assert r.status_code == 200 and escape(SENT) in r.text
    assert "Password reset request failed" in caplog.text
    assert await db.scalar(select(func.count()).select_from(PasswordResetToken)) == 0


async def test_login_page_links_to_forgot_password(client):
    page = await client.get("/login")
    assert re.search(r'<a href="/password/forgot"[^>]*>Forgot password\?</a>', page.text)


async def test_reset_closes_the_users_open_sockets(live_server, db):
    user = await create_user(db, EMAIL)
    board = await create_board(db, user)
    outbox = live_server.app.state.mailer.outbox
    async with (
        httpx.AsyncClient(base_url=live_server.url, headers={"Origin": live_server.url}) as device,
        httpx.AsyncClient(base_url=live_server.url, headers={"Origin": live_server.url}) as browser,
    ):
        await login(device, EMAIL)
        url = live_server.url.replace("http://", "ws://", 1) + f"/ws/boards/{board.id}"
        cookie = {"Cookie": f"{SESSION_COOKIE}={device.cookies[SESSION_COOKIE]}"}
        async with connect(url, origin=live_server.url, additional_headers=cookie) as ws:
            with pytest.raises(TimeoutError):  # admitted: the socket stays open
                await asyncio.wait_for(ws.recv(), 0.5)
            token = await request_link(browser, outbox)
            assert (await set_new_password(browser, token)).status_code == 303

            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 1.0)
            assert ws.close_code == 4401
