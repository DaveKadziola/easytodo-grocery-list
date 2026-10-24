"""Argon2 runs on the app's hashing threads (``argon2*``), not on the thread of the event loop,
whichever flow needs a password hashed or checked."""

import threading

import pytest
from argon2 import PasswordHasher
from factories import DEFAULT_PASSWORD, create_user
from helpers import login

from auth.test_password_reset import request_link, set_new_password
from auth.test_register_login import post_form
from easytodo.admin import service as admin_service
from easytodo.auth import twofa

EMAIL = "dave@example.com"
NEW_PASSWORD = "an-entirely-new-passphrase"


def record_argon2_threads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """From now on, the name of the thread of each argon2 hash and check goes into the list."""
    names: list[str] = []
    real_hash, real_verify = PasswordHasher.hash, PasswordHasher.verify

    def hash_(self, *args, **kwargs):
        names.append(threading.current_thread().name)
        return real_hash(self, *args, **kwargs)

    def verify(self, *args, **kwargs):
        names.append(threading.current_thread().name)
        return real_verify(self, *args, **kwargs)

    monkeypatch.setattr(PasswordHasher, "hash", hash_)
    monkeypatch.setattr(PasswordHasher, "verify", verify)
    return names


@pytest.mark.parametrize(
    "flow",
    [
        "register",
        "login",
        "login_unknown_email",
        "change_password",
        "reset_password",
        "check_password",  # the last two straight through the service
        "admin_create_user",
    ],
)
async def test_argon2_runs_in_the_hashing_threads(flow, client, db, mail_outbox, monkeypatch):
    # Everything a flow needs first is done before the threads are recorded.
    if flow == "register":
        await client.get("/register")
    elif flow in ("login", "login_unknown_email"):
        await create_user(db, EMAIL)
        await client.get("/login")
    elif flow == "change_password":
        await create_user(db, EMAIL)
        await login(client, EMAIL)
    elif flow == "reset_password":
        await create_user(db, EMAIL)
        token = await request_link(client, mail_outbox)
    else:
        user = await create_user(db, EMAIL)
    names = record_argon2_threads(monkeypatch)

    if flow == "register":
        r = await post_form(client, "/register", email="eve@example.com", password=DEFAULT_PASSWORD)
        assert r.status_code == 303
    elif flow == "login":
        r = await post_form(client, "/login", email=EMAIL, password=DEFAULT_PASSWORD)
        assert r.status_code == 303
    elif flow == "login_unknown_email":  # still checked against a dummy hash
        r = await post_form(client, "/login", email="nobody@example.com", password="x" * 12)
        assert r.status_code == 400
    elif flow == "change_password":
        form = {
            "current_password": DEFAULT_PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        r = await client.post("/account/password", data=form)
        assert r.status_code == 303
    elif flow == "reset_password":
        r = await set_new_password(client, token, NEW_PASSWORD)
        assert r.status_code == 303
    elif flow == "check_password":
        await twofa.check_password(user, DEFAULT_PASSWORD)
    else:
        await admin_service.create_user_with_temp_password(db, "eve@example.com")

    assert names and all(name.startswith("argon2") for name in names), names
