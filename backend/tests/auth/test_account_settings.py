"""The settings on the account page: display name, how long archived items are kept, and the
default sort order. Each has its own form and route, that saves all of its fields or none."""

import json
import re
from urllib.parse import unquote

import pytest
from factories import create_user
from helpers import NASTY, NASTY_ESCAPED
from sqlalchemy import select

from auth.test_account import change_form, password_hash, session_id, session_ids, sign_in
from easytodo.auth.account_routes import (
    ARCHIVE_SAVED,
    DISPLAY_NAME_MAX_LENGTH,
    PROFILE_SAVED,
    RETENTION_INVALID,
    SORT_INVALID,
    SORT_SAVED,
)
from easytodo.models import User
from easytodo.security.csrf import CSRF_HEADER
from easytodo.web import FLASH_COOKIE

EMAIL = "dave@example.com"
DEFAULTS = (None, 30, "name", False, True)  # what a new account has, in settings() order

# Every form of this page with data that changes every setting it holds.
SETTINGS_FORMS = [
    ("/account/profile", {"display_name": "Anna Kowalska"}),
    ("/account/archive-retention", {"days": "7"}),
    ("/account/sort", {"sort_field": "updated", "sort_direction": "desc", "sort_open_first": ""}),
]


async def settings(db) -> tuple:
    """The five settings of the one account, as ``DEFAULTS`` lists them."""
    db.expire_all()
    row = (
        await db.execute(
            select(
                User.display_name,
                User.archive_retention_days,
                User.sort_field,
                User.sort_descending,
                User.sort_open_first,
            )
        )
    ).one()
    return tuple(row)


def flash_message(response) -> str:
    return json.loads(unquote(response.cookies[FLASH_COOKIE]))["message"]


async def test_account_page_shows_the_defaults(account_client):
    r = await account_client.get("/account")

    assert r.status_code == 200
    for selected in ('value="30"', 'value="name"', 'value="asc"'):
        assert f"<option {selected} selected>" in r.text
    assert re.search(r'id="sort-open-first"[^>]* checked>', r.text)
    assert "Shown as “dave” if left empty." in r.text
    assert 'placeholder="dave"' in r.text
    assert 'value="None"' not in r.text
    assert (
        "Archived boards and categories are deleted permanently after this time. "
        "For a board and its categories, the board owner's setting counts."
    ) in r.text
    assert "Used in every category unless you set a category's own sort order." in r.text


async def test_display_name_is_cleaned_saved_and_flashed(account_client, db):
    r = await account_client.post("/account/profile", data={"display_name": "  Anna   Kowalska "})

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert flash_message(r) == PROFILE_SAVED == "Profile saved."
    assert (await settings(db))[0] == "Anna Kowalska"
    page = await account_client.get("/account")
    assert 'value="Anna Kowalska"' in page.text

    longest = "x" * DISPLAY_NAME_MAX_LENGTH
    r = await account_client.post("/account/profile", data={"display_name": longest})
    assert r.status_code == 303 and (await settings(db))[0] == longest

    r = await account_client.post("/account/profile", data={"display_name": "   "})
    assert r.status_code == 303 and (await settings(db))[0] is None  # back to the email's part


@pytest.mark.parametrize("value", ["x" * 61, "\x00"])
async def test_invalid_display_name_is_400(account_client, db, value):
    await account_client.post("/account/profile", data={"display_name": "Anna"})

    r = await account_client.post("/account/profile", data={"display_name": value})

    assert r.status_code == 400
    assert "Display name must be 1–60 characters." in r.text
    assert FLASH_COOKIE not in r.headers.get("set-cookie", "")
    assert (await settings(db))[0] == "Anna"
    assert 'value="Anna"' in r.text  # the page shows what is saved, not the rejected text


async def test_display_name_is_escaped_on_the_account_page(client, db):
    await create_user(db, "o'reilly@example.com", display_name=NASTY)
    await sign_in(client, "o'reilly@example.com")

    r = await client.get("/account")

    assert f'value="{NASTY_ESCAPED}"' in r.text
    assert "Shown as “o&#39;reilly” if left empty." in r.text
    assert "<script>alert(1)" not in r.text and "<b>" not in r.text


@pytest.mark.parametrize(
    ("days", "stored"), [("7", 7), ("30", 30), ("90", 90), ("365", 365), ("never", None)]
)
async def test_archive_retention_is_saved(account_client, db, days, stored):
    if days == "30":  # from another value, so that saving it is a change
        await account_client.post("/account/archive-retention", data={"days": "7"})

    r = await account_client.post("/account/archive-retention", data={"days": days})

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert flash_message(r) == ARCHIVE_SAVED == "Archive settings saved."
    assert (await settings(db))[1] == stored  # "never" is NULL, not 30
    page = await account_client.get("/account")
    assert f'<option value="{days}" selected>' in page.text


@pytest.mark.parametrize("days", ["14", "", "forever"])
async def test_invalid_archive_retention_is_400(account_client, db, days):
    r = await account_client.post("/account/archive-retention", data={"days": days})

    assert r.status_code == 400
    assert RETENTION_INVALID == "Choose how long archived items are kept."
    assert RETENTION_INVALID in r.text
    assert FLASH_COOKIE not in r.headers.get("set-cookie", "")
    assert (await settings(db))[1] == 30


async def test_missing_archive_retention_is_400(account_client, db):
    r = await account_client.post("/account/archive-retention", data={})

    assert r.status_code == 400 and RETENTION_INVALID in r.text
    assert (await settings(db))[1] == 30


async def test_default_sort_is_saved(account_client, db):
    r = await account_client.post(
        "/account/sort", data={"sort_field": "updated", "sort_direction": "desc"}
    )

    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert flash_message(r) == SORT_SAVED == "Sorting saved."
    assert (await settings(db))[2:] == ("updated", True, False)
    page = await account_client.get("/account")
    assert '<option value="updated" selected>' in page.text
    assert '<option value="desc" selected>' in page.text
    assert not re.search(r'id="sort-open-first"[^>]* checked>', page.text)

    r = await account_client.post(
        "/account/sort",
        data={"sort_field": "created", "sort_direction": "asc", "sort_open_first": "true"},
    )

    assert r.status_code == 303
    assert (await settings(db))[2:] == ("created", False, True)
    page = await account_client.get("/account")
    assert re.search(r'id="sort-open-first"[^>]* checked>', page.text)


@pytest.mark.parametrize(
    "data",
    [
        SETTINGS_FORMS[2][1] | {"sort_field": "size"},
        SETTINGS_FORMS[2][1] | {"sort_direction": "up"},
        SETTINGS_FORMS[2][1] | {"sort_open_first": "maybe"},
        {},
    ],
    ids=["field", "direction", "open-first", "nothing"],
)
async def test_invalid_sort_is_400(account_client, db, data):
    """One wrong value rejects the whole form: the valid fields next to it are not saved."""
    r = await account_client.post("/account/sort", data=data)

    assert r.status_code == 400
    assert SORT_INVALID == "Choose a valid sort order."
    assert SORT_INVALID in r.text
    assert FLASH_COOKIE not in r.headers.get("set-cookie", "")
    assert (await settings(db)) == DEFAULTS


@pytest.mark.parametrize(("path", "data"), SETTINGS_FORMS)
async def test_forced_password_change_blocks_the_settings_forms(client, db, path, data):
    await create_user(db, EMAIL, must_change_password=True)
    await sign_in(client)

    r = await client.post(path, data=data)

    assert r.status_code == 403
    assert (await settings(db)) == DEFAULTS


ACCOUNT_POSTS = [
    *SETTINGS_FORMS,
    ("/account/password", change_form()),
    ("/account/sessions/revoke-others", {}),
    ("/account/sessions/{other}/revoke", {}),
]


@pytest.mark.parametrize("attack", ["no-token", "foreign-origin"])
@pytest.mark.parametrize(("path", "data"), ACCOUNT_POSTS)
async def test_account_posts_need_the_token_and_own_origin(client_factory, db, path, data, attack):
    await create_user(db, EMAIL)
    victim, other = await sign_in(await client_factory()), await sign_in(await client_factory())
    path = path.format(other=await session_id(db, other))
    headers = {}
    if attack == "no-token":
        del victim.headers[CSRF_HEADER]
    else:
        headers["Origin"] = "https://evil.example"

    async def unchanged() -> tuple:
        db.expire_all()
        return await password_hash(db), await settings(db), await session_ids(db)

    before = await unchanged()

    r = await victim.post(path, data=data, headers=headers)

    assert r.status_code == 403
    assert (
        "Invalid CSRF token." if attack == "no-token" else "Open the app at http://testserver."
    ) in r.text
    assert await unchanged() == before
