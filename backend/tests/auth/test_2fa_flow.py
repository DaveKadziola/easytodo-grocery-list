"""Two-factor sign-in (a TOTP code or a recovery code after the password) and the account pages
that turn it on and off."""

import asyncio
import html
import json
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pyotp
import pytest
from factories import DEFAULT_PASSWORD, create_board, create_user
from helpers import login
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from easytodo.auth import routes
from easytodo.auth.sessions import SESSION_COOKIE, create_session
from easytodo.auth.twofa import (
    recovery_code_matches,
    regenerate_recovery_codes,
    verify_second_factor,
)
from easytodo.models import RecoveryCode, User, UserSession
from easytodo.security.crypto import decrypt
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.security.ratelimit import RateLimiter
from easytodo.security.tokens import hash_token
from easytodo.web import FLASH_COOKIE

EMAIL = "dave@example.com"
WRONG_PASSWORD = "not-the-right-password"
INVALID_CODE = "Invalid code. Try again."
WRONG_CURRENT_PASSWORD = "Current password is incorrect."
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
SECRET_ON_PAGE = re.compile(r'id="totp-secret"[^>]*>([A-Z2-7]+)<')
RECOVERY_CODES_ON_PAGE = re.compile(r"<li data-recovery-code>([^<]+)</li>")
RECOVERY_TEXT = re.compile(
    r'<textarea id="recovery-codes-text" readonly class="sr-only" aria-hidden="true"'
    r' tabindex="-1">([^<]*)</textarea>'
)
SAVE_BUTTON = (
    '<button type="button" class="btn-secondary" data-download="recovery-codes-text"'
    ' data-filename="easytodo-recovery-codes.txt">Save as .txt</button>'
)
COPY_BUTTON = (
    '<button type="button" class="btn-secondary" data-copy="recovery-codes-text">Copy</button>'
)
TOO_MANY_REQUESTS = "Too many requests. Try again later."
HX = {"HX-Request": "true"}


def current_code(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def next_code(secret: str) -> str:
    """The next time step's code: still accepted (the window is one step each way) after a code
    of the current step was used."""
    return pyotp.TOTP(secret).at(time.time() + 30)


def wrong_code(secret: str) -> str:
    """Six digits that no time step near now accepts."""
    totp = pyotp.TOTP(secret)
    near = {totp.at(time.time() + offset) for offset in (-60, -30, 0, 30, 60)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in near)


async def password_step(
    client: httpx.AsyncClient, *, next_: str | None = None, user_agent: str | None = None
) -> httpx.Response:
    """Submit the sign-in form as dave; later requests from ``client`` carry the CSRF header."""
    if user_agent:
        client.headers["User-Agent"] = user_agent
    await client.get("/login")  # issues the CSRF cookie
    client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    params = {"next": next_} if next_ else None
    return await client.post(
        "/login", params=params, data={"email": EMAIL, "password": DEFAULT_PASSWORD}
    )


async def code_step(
    client: httpx.AsyncClient, code: str, *, next_: str | None = None
) -> httpx.Response:
    params = {"next": next_} if next_ else None
    return await client.post("/login/2fa", params=params, data={"code": code})


async def sign_in_with_2fa(client: httpx.AsyncClient, secret: str) -> None:
    await password_step(client)
    r = await code_step(client, current_code(secret))
    assert r.status_code == 303, r.text


async def enable_2fa(client: httpx.AsyncClient) -> tuple[str, list[str]]:
    """Turn 2FA on through the account pages; returns the secret and the recovery codes."""
    page = await client.get("/account/2fa/setup")
    secret = SECRET_ON_PAGE.search(page.text)[1]
    form = {"password": DEFAULT_PASSWORD, "code": current_code(secret)}
    r = await client.post("/account/2fa/setup", data=form)
    assert r.status_code == 200, r.text
    return secret, RECOVERY_CODES_ON_PAGE.findall(r.text)


async def dave(db) -> User:
    """Dave's row as the database has it now."""
    query = select(User).where(User.email == EMAIL).execution_options(populate_existing=True)
    return await db.scalar(query)


async def sessions(db) -> list[UserSession]:
    query = select(UserSession).order_by(UserSession.id)
    return list(await db.scalars(query.execution_options(populate_existing=True)))


async def recovery_code_hashes(db) -> set[str]:
    return set(await db.scalars(select(RecoveryCode.code_hash)))


async def stored_codes_are(db, codes: list[str]) -> bool:
    """Whether the stored recovery-code hashes are exactly those of ``codes``."""
    stored = await recovery_code_hashes(db)
    return len(stored) == len(codes) and all(
        any(recovery_code_matches(code, h) for h in stored) for code in codes
    )


async def unused_recovery_codes(db) -> int:
    query = select(func.count()).select_from(RecoveryCode).where(RecoveryCode.used_at.is_(None))
    return await db.scalar(query)


async def test_enable_2fa_and_login_requires_code(client_factory, db):
    await create_user(db, EMAIL)
    owner = await client_factory()
    await login(owner, EMAIL)
    assert 'href="/account/2fa/setup"' in (await owner.get("/account")).text

    page = await owner.get("/account/2fa/setup")
    assert page.status_code == 200
    assert "<svg" in page.text  # the QR code, inline
    assert page.headers["cache-control"] == "no-store"
    assert 'name="password" type="password"' in page.text
    secret = SECRET_ON_PAGE.search(page.text)[1]
    form = {"password": DEFAULT_PASSWORD, "code": current_code(secret)}
    r = await owner.post("/account/2fa/setup", data=form)

    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    codes = RECOVERY_CODES_ON_PAGE.findall(r.text)
    assert len(set(codes)) == 10
    half = f"[{RECOVERY_ALPHABET}]{{4}}"
    assert all(re.fullmatch(f"{half}-{half}", code) for code in codes), codes
    assert await stored_codes_are(db, codes)
    assert (await dave(db)).totp_enabled is True
    assert 'action="/account/2fa/disable"' in (await owner.get("/account")).text

    device = await client_factory()
    r = await password_step(device)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    r = await device.get("/boards")
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    pending = next(s for s in await sessions(db) if s.mfa_pending)
    assert pending.token_hash == hash_token(device.cookies[SESSION_COOKIE])
    page = await device.get("/login/2fa")
    assert page.status_code == 200 and 'name="code"' in page.text


async def test_second_step_rotates_session(client, db, user_with_2fa):
    await password_step(client)
    pending_token = client.cookies[SESSION_COOKIE]

    r = await code_step(client, current_code(user_with_2fa.secret))

    assert r.status_code == 303 and r.headers["location"] == "/boards"
    full_token = client.cookies[SESSION_COOKIE]
    assert full_token != pending_token
    assert [(s.token_hash, s.mfa_pending) for s in await sessions(db)] == [
        (hash_token(full_token), False)
    ]
    assert (await client.get("/boards")).status_code == 200

    client.cookies.delete(SESSION_COOKIE)
    client.cookies.set(SESSION_COOKIE, pending_token)
    r = await client.get("/boards")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fboards"


@pytest.mark.parametrize(
    ("next_", "second_step", "target"),
    [
        (
            "/account?tab=sessions",
            "/login/2fa?next=%2Faccount%3Ftab%3Dsessions",
            "/account?tab=sessions",
        ),
        ("https://evil.example", "/login/2fa", "/boards"),
        ("//evil.example", "/login/2fa", "/boards"),
        (None, "/login/2fa", "/boards"),
    ],
)
async def test_second_step_leads_to_next(client, user_with_2fa, next_, second_step, target):
    r = await password_step(client, next_=next_)
    assert r.status_code == 303 and r.headers["location"] == second_step

    r = await code_step(client, current_code(user_with_2fa.secret), next_=next_)

    assert r.status_code == 303 and r.headers["location"] == target


async def test_recovery_code_works_once(client_factory, db):
    await create_user(db, EMAIL)
    owner = await client_factory()
    await login(owner, EMAIL)
    _, codes = await enable_2fa(owner)

    first = await client_factory()
    await password_step(first)
    r = await code_step(first, codes[0])
    assert r.status_code == 303 and r.headers["location"] == "/boards"
    rows = await db.execute(select(RecoveryCode.code_hash, RecoveryCode.used_at))
    used = [code_hash for code_hash, used_at in rows if used_at is not None]
    assert len(used) == 1 and recovery_code_matches(codes[0], used[0])

    second = await client_factory()
    await password_step(second)
    r = await code_step(second, codes[0])
    assert r.status_code == 400 and INVALID_CODE in r.text
    r = await code_step(second, codes[1])
    assert r.status_code == 303 and r.headers["location"] == "/boards"
    assert await unused_recovery_codes(db) == 8


@pytest.mark.parametrize(
    "typed",
    [str.upper, lambda code: f" {code[:4]} - {code[5:]} ", lambda code: code.replace("-", "")],
    ids=["capitals", "spaces", "no-hyphen"],
)
async def test_recovery_code_may_be_typed_loosely(client, db, user_with_2fa, typed):
    await password_step(client)
    r = await code_step(client, typed(user_with_2fa.recovery_codes[0]))
    assert r.status_code == 303 and r.headers["location"] == "/boards"
    assert await unused_recovery_codes(db) == 9


async def test_a_totp_code_signs_in_only_once(client_factory, user_with_2fa):
    code = current_code(user_with_2fa.secret)
    first = await client_factory()
    await password_step(first)
    assert (await code_step(first, code)).status_code == 303

    second = await client_factory()
    await password_step(second)
    r = await code_step(second, code)
    assert r.status_code == 400 and INVALID_CODE in r.text


async def test_a_totp_code_sent_twice_at_once_counts_once(db, settings, user_with_2fa):
    user, code = user_with_2fa.user, current_code(user_with_2fa.secret)
    async with AsyncSession(db.bind, expire_on_commit=False) as other_request:
        # Loaded before the first request used the code: it has not seen the step used yet.
        same_user = await other_request.get_one(User, user.id)

        assert await verify_second_factor(db, user, code, settings) is True
        assert await verify_second_factor(other_request, same_user, code, settings) is False


async def test_five_bad_codes_kill_pending_session(client, db, user_with_2fa):
    await password_step(client)
    pending_token = client.cookies[SESSION_COOKIE]
    wrong = wrong_code(user_with_2fa.secret)
    for _ in range(4):
        r = await code_step(client, wrong)
        assert r.status_code == 400 and INVALID_CODE in r.text
    assert [s.mfa_failures for s in await sessions(db)] == [4]

    r = await code_step(client, wrong)

    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert json.loads(unquote(r.cookies[FLASH_COOKIE])) == {
        "level": "error",
        "message": "Too many invalid codes. Sign in again.",
    }
    assert SESSION_COOKIE not in client.cookies
    assert await sessions(db) == []
    # The deleted session's cookie gets nowhere, even with the right code.
    client.cookies.set(SESSION_COOKIE, pending_token)
    r = await code_step(client, current_code(user_with_2fa.secret))
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert await sessions(db) == []


async def test_pending_sign_in_expires_after_15_minutes(client, db, user_with_2fa):
    await password_step(client)
    [pending] = await sessions(db)
    assert timedelta(minutes=14) < pending.expires_at - datetime.now(UTC) <= timedelta(minutes=15)
    await code_step(client, current_code(user_with_2fa.secret))
    [full] = await sessions(db)
    assert full.expires_at - datetime.now(UTC) > timedelta(days=29)


async def test_a_new_password_step_ends_the_abandoned_one(client, db, user_with_2fa):
    await password_step(client)
    [abandoned] = await sessions(db)
    await password_step(client)  # the same browser
    [pending] = await sessions(db)  # not 2 rows of "Awaiting 2FA"
    assert pending.id != abandoned.id and pending.mfa_pending


async def test_a_wrong_password_leaves_the_pending_sign_in_alone(client, db, user_with_2fa):
    await password_step(client)
    [pending] = await sessions(db)
    token = client.cookies[SESSION_COOKIE]

    r = await client.post("/login", data={"email": EMAIL, "password": WRONG_PASSWORD})

    assert r.status_code == 400 and "Invalid email or password." in r.text
    assert [s.id for s in await sessions(db)] == [pending.id]
    assert client.cookies[SESSION_COOKIE] == token
    r = await code_step(client, current_code(user_with_2fa.secret))  # the code step still works
    assert r.status_code == 303 and r.headers["location"] == "/boards"


async def test_a_sign_in_as_another_user_ends_the_pending_one(client, db, user_with_2fa):
    await create_user(db, "eve@example.com")
    await password_step(client)
    [pending] = await sessions(db)

    await login(client, "eve@example.com")

    [eves] = await sessions(db)
    assert eves.id != pending.id and eves.mfa_pending is False
    assert eves.user_id != user_with_2fa.user.id


async def test_a_new_sign_in_leaves_a_full_session_alone(client, db, user_with_2fa):
    await sign_in_with_2fa(client, user_with_2fa.secret)
    [full] = await sessions(db)

    await password_step(client)  # another sign-in from the same browser

    first, second = await sessions(db)
    assert first.id == full.id and (first.mfa_pending, second.mfa_pending) == (False, True)


async def test_a_code_over_the_limit_is_not_checked(client, db, user_with_2fa):
    await password_step(client)
    await db.execute(update(UserSession).values(mfa_failures=5))
    await db.commit()

    r = await code_step(client, current_code(user_with_2fa.secret))  # even the right code

    assert r.status_code == 303 and r.headers["location"] == "/login"
    flash = json.loads(unquote(r.cookies[FLASH_COOKIE]))
    assert flash["message"] == "Too many invalid codes. Sign in again."
    assert (await dave(db)).totp_last_step is None
    assert await sessions(db) == []


async def test_parallel_codes_are_checked_at_most_five_times(
    client, db, user_with_2fa, monkeypatch
):
    await password_step(client)
    checked, real = [], routes.verify_second_factor

    async def slow(*args):
        checked.append(1)
        await asyncio.sleep(0.05)
        return await real(*args)

    monkeypatch.setattr(routes, "verify_second_factor", slow)
    wrong = wrong_code(user_with_2fa.secret)

    responses = await asyncio.gather(*(code_step(client, wrong) for _ in range(10)))  # 10: IP limit

    assert len(checked) == 5  # not 10
    assert sorted(r.status_code for r in responses) == [303] * 6 + [400] * 4


async def test_second_step_is_rate_limited_per_ip(client, user_with_2fa):
    await password_step(client)
    wrong = wrong_code(user_with_2fa.secret)
    statuses = [(await code_step(client, wrong)).status_code for _ in range(11)]
    # The fifth wrong code ends the pending session; then the form only sends back to /login.
    assert statuses == [400] * 4 + [303] * 6 + [429]


async def test_second_step_codes_are_limited_per_account(app, client_from_ip, db, user_with_2fa):
    """Signing in again with the password, from other IPs, brings no more guesses: the account has
    20 codes per 15 minutes over all its pending sign-ins."""
    now = [0.0]
    app.state.rate_limiter = RateLimiter(clock=lambda: now[0])
    wrong = wrong_code(user_with_2fa.secret)
    for n in range(5):  # 5 fresh pending sign-ins x 4 wrong codes = 20, each from its own IP
        device = await client_from_ip(f"10.0.0.{n + 1}")
        await password_step(device)
        for _ in range(4):
            r = await code_step(device, wrong)
            assert r.status_code == 400 and INVALID_CODE in r.text

    device = await client_from_ip("10.0.0.99")
    r = await password_step(device)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    r = await code_step(device, current_code(user_with_2fa.secret))  # even the right code

    assert r.status_code == 429
    assert r.headers["content-type"].startswith("text/html")
    assert TOO_MANY_REQUESTS in r.text and 'name="code"' in r.text  # the code form, for later
    assert (await dave(db)).totp_last_step is None  # the code was not even checked
    pending = await sessions(db)
    assert len(pending) == 6 and all(s.mfa_pending for s in pending)
    assert [s.mfa_failures for s in pending][-1] == 0  # a refused attempt is not a wrong code

    now[0] = 899.0  # the window is 15 minutes, counted from the first code
    assert (await code_step(device, current_code(user_with_2fa.secret))).status_code == 429
    now[0] = 900.0
    r = await code_step(device, current_code(user_with_2fa.secret))
    assert r.status_code == 303 and r.headers["location"] == "/boards"


async def test_pending_session_signed_out_meanwhile_ends_the_second_step(
    client, db, user_with_2fa, monkeypatch
):
    await password_step(client)
    real = routes.reserve_code_attempt

    async def signed_out_from_the_account_page_meanwhile(*args) -> int | None:
        await db.execute(delete(UserSession))
        await db.commit()
        return await real(*args)  # None: the session is gone

    monkeypatch.setattr(routes, "reserve_code_attempt", signed_out_from_the_account_page_meanwhile)
    r = await code_step(client, wrong_code(user_with_2fa.secret))

    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert SESSION_COOKIE not in client.cookies
    assert FLASH_COOKIE not in r.cookies


async def test_recovery_codes_still_sign_in_after_the_secret_key_changes(
    client_factory, user_with_2fa, caplog
):
    device = await client_factory(secret_key="another-secret-key-" + "y" * 30)
    await password_step(device)

    r = await code_step(device, current_code(user_with_2fa.secret))
    assert r.status_code == 400 and INVALID_CODE in r.text
    assert "cannot be decrypted" in caplog.text
    r = await code_step(device, user_with_2fa.recovery_codes[0])
    assert r.status_code == 303 and r.headers["location"] == "/boards"


async def test_second_step_needs_a_pending_session(client, db):
    assert (await client.get("/login/2fa")).headers["location"] == "/login"
    await create_user(db, EMAIL)
    await login(client, EMAIL)  # no 2FA: a full session

    for r in (await client.get("/login/2fa"), await code_step(client, "123456")):
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.get("/boards")).status_code == 200


async def test_totp_secret_is_encrypted_at_rest(db, settings, user_with_2fa):
    stored = await db.scalar(select(User.totp_secret_enc).where(User.id == user_with_2fa.user.id))
    assert stored != user_with_2fa.secret and user_with_2fa.secret not in stored
    assert decrypt(stored, settings) == user_with_2fa.secret
    # Recovery codes are stored only as hashes: salted and slow (scrypt), not a plain SHA-256,
    # which one GPU pass over their ~2^40 values would undo for every user of a stolen dump.
    stored = await recovery_code_hashes(db)
    assert await stored_codes_are(db, user_with_2fa.recovery_codes)
    for code in user_with_2fa.recovery_codes:
        assert hash_token(code) not in stored
        assert code not in "".join(stored)
    salts = {h[:32] for h in stored}
    assert len(salts) == 1  # one salt per set of codes: a sign-in works out one hash, not ten
    await regenerate_recovery_codes(db, user_with_2fa.user, DEFAULT_PASSWORD)
    assert {h[:32] for h in await recovery_code_hashes(db)}.isdisjoint(salts)  # a new one each time


async def test_wrong_setup_code_keeps_2fa_off(client, db):
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    secret = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]

    form = {"password": DEFAULT_PASSWORD, "code": wrong_code(secret)}
    r = await client.post("/account/2fa/setup", data=form)

    assert r.status_code == 400 and INVALID_CODE in r.text
    assert SECRET_ON_PAGE.search(r.text)[1] == secret  # the same key: nothing to scan again
    assert (await dave(db)).totp_enabled is False
    assert await recovery_code_hashes(db) == set()
    form = {"password": DEFAULT_PASSWORD, "code": current_code(secret)}
    r = await client.post("/account/2fa/setup", data=form)
    assert r.status_code == 200


async def test_setup_needs_the_password(client, db):
    """A stolen session alone cannot turn 2FA on, which would lock the owner out."""
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    secret = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]

    for form in (
        {"password": WRONG_PASSWORD, "code": current_code(secret)},
        {"code": current_code(secret)},
    ):
        r = await client.post("/account/2fa/setup", data=form)

        assert r.status_code == 400 and WRONG_CURRENT_PASSWORD in r.text
        assert SECRET_ON_PAGE.search(r.text)[1] == secret  # the same key, still not turned on
        assert r.headers["cache-control"] == "no-store"
        user = await dave(db)
        assert (user.totp_enabled, user.totp_last_step) == (False, None)  # the code not used up
        assert await recovery_code_hashes(db) == set()

    form = {"password": DEFAULT_PASSWORD, "code": current_code(secret)}
    r = await client.post("/account/2fa/setup", data=form)

    assert r.status_code == 200
    assert len(set(RECOVERY_CODES_ON_PAGE.findall(r.text))) == 10
    assert (await dave(db)).totp_enabled is True


async def test_setup_shares_the_password_guess_budget(client, db):
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    secret = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]
    form = {"password": WRONG_PASSWORD, "code": current_code(secret)}
    statuses = [(await client.post("/account/2fa/setup", data=form)).status_code for _ in range(10)]
    assert statuses == [400] * 10

    # The change-password form, the 2FA forms and this one have one budget per account.
    r = await client.post("/account/2fa/setup", data=form | {"password": DEFAULT_PASSWORD})
    assert r.status_code == 429
    r = await client.post("/account/password", data={"current_password": DEFAULT_PASSWORD})
    assert r.status_code == 429
    assert (await dave(db)).totp_enabled is False


async def test_setup_is_refused_while_2fa_is_on(client, db, user_with_2fa):
    await sign_in_with_2fa(client, user_with_2fa.secret)
    before = (await dave(db)).totp_secret_enc

    code = next_code(user_with_2fa.secret)
    for r in (
        await client.get("/account/2fa/setup"),
        await client.post("/account/2fa/setup", data={"password": DEFAULT_PASSWORD, "code": code}),
        await client.post("/account/2fa/setup", data={"password": WRONG_PASSWORD, "code": code}),
    ):
        assert r.status_code == 409
        assert "Two-factor authentication is already on." in r.text
        assert user_with_2fa.secret not in r.text  # the secret in use is never shown again
    user = await dave(db)
    assert (user.totp_enabled, user.totp_secret_enc) == (True, before)


async def test_disable_2fa_requires_password_and_code(client, db, user_with_2fa):
    secret = user_with_2fa.secret
    await sign_in_with_2fa(client, secret)

    for form, message in [
        ({"password": WRONG_PASSWORD, "code": next_code(secret)}, WRONG_CURRENT_PASSWORD),
        ({"password": DEFAULT_PASSWORD, "code": wrong_code(secret)}, INVALID_CODE),
        ({"password": DEFAULT_PASSWORD}, INVALID_CODE),
    ]:
        r = await client.post("/account/2fa/disable", data=form)
        assert r.status_code == 400 and message in r.text
        assert (await dave(db)).totp_enabled is True

    # The code sent with the wrong password above was not used up.
    form = {"password": DEFAULT_PASSWORD, "code": next_code(secret)}
    r = await client.post("/account/2fa/disable", data=form)

    assert r.status_code == 303 and r.headers["location"] == "/account"
    user = await dave(db)
    assert (user.totp_enabled, user.totp_secret_enc, user.totp_last_step) == (False, None, None)
    assert await recovery_code_hashes(db) == set()
    assert 'href="/account/2fa/setup"' in (await client.get("/account")).text
    await client.post("/logout")
    r = await password_step(client)
    assert r.status_code == 303 and r.headers["location"] == "/"  # no second step any more


async def test_disable_2fa_accepts_a_recovery_code(client, db, user_with_2fa):
    await sign_in_with_2fa(client, user_with_2fa.secret)
    form = {"password": DEFAULT_PASSWORD, "code": user_with_2fa.recovery_codes[0]}
    r = await client.post("/account/2fa/disable", data=form)
    assert r.status_code == 303
    assert (await dave(db)).totp_enabled is False


async def test_new_recovery_codes_require_the_password(client, db, user_with_2fa):
    await sign_in_with_2fa(client, user_with_2fa.secret)
    before = await recovery_code_hashes(db)

    r = await client.post("/account/2fa/recovery-codes", data={"password": WRONG_PASSWORD})
    assert r.status_code == 400 and WRONG_CURRENT_PASSWORD in r.text
    assert await recovery_code_hashes(db) == before

    r = await client.post("/account/2fa/recovery-codes", data={"password": DEFAULT_PASSWORD})

    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    codes = RECOVERY_CODES_ON_PAGE.findall(r.text)
    assert len(set(codes)) == 10
    assert await stored_codes_are(db, codes)  # old ones gone


def offered_codes(r: httpx.Response, before: datetime) -> list[str]:
    """The recovery codes of a page that shows them, after checking the text offered for saving
    and copying: the codes with the account and the time they were made, and the buttons."""
    codes = RECOVERY_CODES_ON_PAGE.findall(r.text)
    content = html.unescape(RECOVERY_TEXT.search(r.text)[1])
    created = datetime.strptime(
        re.search(r"^Created: (.+) UTC$", content, re.MULTILINE)[1], "%Y-%m-%d %H:%M"
    ).replace(tzinfo=UTC)
    assert before.replace(second=0, microsecond=0) <= created <= datetime.now(UTC)
    assert content == (
        f"EasyToDo recovery codes\nAccount: {EMAIL}\nCreated: {created:%Y-%m-%d %H:%M} UTC\n\n"
        + "\n".join(codes)
        + "\n\nEach code signs you in once.\n"
    )
    assert SAVE_BUTTON in r.text and COPY_BUTTON in r.text
    assert r.headers["cache-control"] == "no-store"
    return codes


async def test_recovery_page_offers_save_and_copy(client, db):
    # No stored_codes_are here (scrypt, about 2 s a code): the tests above cover the hashes.
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    secret = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]
    before = datetime.now(UTC)
    form = {"password": DEFAULT_PASSWORD, "code": current_code(secret)}
    r = await client.post("/account/2fa/setup", data=form)
    first = offered_codes(r, before)
    assert len(first) == len(set(first)) == 10  # the <li> items only, not the text

    before = datetime.now(UTC)
    r = await client.post("/account/2fa/recovery-codes", data={"password": DEFAULT_PASSWORD})
    second = offered_codes(r, before)  # the other place that renders the page
    assert len(second) == 10 and not set(first) & set(second)


async def test_pending_session_reaches_only_the_second_step_and_logout(
    client, db, settings, user_with_2fa
):
    _, other_device = await create_session(
        db, user_with_2fa.user, mfa_pending=False, user_agent=None, ip=None, settings=settings
    )
    await password_step(client)

    for path in ("/", "/boards", "/account", "/account/password"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login/2fa", path
    r = await client.get("/boards", headers=HX)
    assert r.headers["hx-redirect"] == "/login/2fa"

    before = [s.id for s in await sessions(db)]
    r = await client.post("/account/sessions/revoke-others", headers=HX)
    assert r.status_code == 401
    r = await client.post("/account/sessions/revoke-others")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert [s.id for s in await sessions(db)] == before

    r = await client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert [s.id for s in await sessions(db)] == [other_device.id]


async def test_must_change_password_comes_after_the_second_step(client, db, user_with_2fa):
    await db.execute(update(User).values(must_change_password=True))
    await db.commit()

    r = await password_step(client)
    assert r.headers["location"] == "/login/2fa"
    assert (await client.get("/account/password")).headers["location"] == "/login/2fa"
    r = await code_step(client, current_code(user_with_2fa.secret))
    assert r.status_code == 303 and r.headers["location"] == "/boards"
    assert (await client.get("/boards")).headers["location"] == "/account/password"


async def test_account_page_labels_sessions_awaiting_2fa(client_factory, db, user_with_2fa):
    owner = await client_factory()
    await sign_in_with_2fa(owner, user_with_2fa.secret)
    stranger = await client_factory()
    await password_step(stranger, user_agent="Stranger Browser")
    stranger_id = next(s.id for s in await sessions(db) if s.mfa_pending)

    page = await owner.get("/account")

    assert page.text.count("Awaiting 2FA") == 1
    assert "Stranger Browser" in page.text
    assert f'action="/account/sessions/{stranger_id}/revoke"' in page.text  # can be signed out


async def test_pending_session_cannot_open_websocket(live_server, db, user_with_2fa):
    board = await create_board(db, user_with_2fa.user)
    async with httpx.AsyncClient(
        base_url=live_server.url, headers={"Origin": live_server.url}
    ) as device:
        r = await password_step(device)
        assert r.headers["location"] == "/login/2fa"
        url = live_server.url.replace("http://", "ws://", 1) + f"/ws/boards/{board.id}"
        cookie = {"Cookie": f"{SESSION_COOKIE}={device.cookies[SESSION_COOKIE]}"}

        async with connect(url, origin=live_server.url, additional_headers=cookie) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 1.0)
            assert ws.close_code == 4401


async def test_reloading_the_setup_page_keeps_the_secret_just_scanned(client, db):
    """A reload, a second tab or the back button (the page is no-store, so it is fetched again)
    must not replace the key the user has just put in their authenticator app."""
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    first = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]

    again = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]

    assert again == first
    form = {"password": DEFAULT_PASSWORD, "code": current_code(first)}
    assert (await client.post("/account/2fa/setup", data=form)).status_code == 200


async def test_setup_after_the_secret_key_changed_starts_over_without_a_500(
    client, client_factory, db
):
    await create_user(db, EMAIL)
    await login(client, EMAIL)
    old = SECRET_ON_PAGE.search((await client.get("/account/2fa/setup")).text)[1]
    device = await client_factory(secret_key="another-secret-key-" + "y" * 30)
    await login(device, EMAIL)

    form = {"password": DEFAULT_PASSWORD, "code": current_code(old)}
    r = await device.post("/account/2fa/setup", data=form)  # the old secret cannot be decrypted
    assert r.status_code == 400 and INVALID_CODE in r.text
    new = SECRET_ON_PAGE.search(r.text)[1]
    assert new != old
    page = await device.get("/account/2fa/setup")
    assert page.status_code == 200 and SECRET_ON_PAGE.search(page.text)[1] == new
