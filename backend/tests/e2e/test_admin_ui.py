"""The admin panel in Chromium: the user list (a table on a desktop, cards on a phone), its
actions answering with the user's row in place, and creating an account, with no CSP
violations."""

import re
from collections.abc import Callable

import pytest
from factories import create_board, create_user
from playwright.sync_api import Page, expect

from e2e.conftest import PHONE, watch_console

pytestmark = pytest.mark.e2e

ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
def admin_page(seed, signed_in_as) -> Callable[..., Page]:
    """``admin_page(**context_options)`` -> a page signed in as the admin admin@example.com."""
    seed(lambda db: create_user(db, ADMIN_EMAIL, is_admin=True))
    return lambda **context_options: signed_in_as(ADMIN_EMAIL, **context_options)


def test_admin_disables_and_deletes_a_user_in_the_list(admin_page, seed):
    async def make(db) -> int:
        dave = await create_user(db, "dave@example.com")
        await create_board(db, dave, "Dave's list")
        return dave.id

    dave_id = seed(make)
    page = admin_page()
    problems = watch_console(page)
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))

    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page.locator("dt", has_text="Users")).to_be_visible()
    page.get_by_role("link", name="Manage users").click()
    expect(page.locator("thead")).to_be_visible()  # a table on a desktop
    row = page.locator(f"#user-{dave_id}")

    row.get_by_role("button", name="Disable").click()
    expect(row.get_by_text("Disabled", exact=True)).to_be_visible()
    expect(page.locator("#toasts")).to_contain_text("Account disabled.")
    row.get_by_role("button", name="Delete").click()
    expect(row).to_have_count(0)
    expect(page.locator("#toasts")).to_contain_text("Account deleted.")

    assert dialogs == [
        (
            "Disable dave@example.com? They are signed out everywhere and can't sign in until "
            "you enable the account again."
        ),
        "Delete dave@example.com and the 1 board they own? This can't be undone.",
    ]
    assert problems == []


def test_admin_creates_an_account_on_a_phone(admin_page):
    page = admin_page(**PHONE)
    problems = watch_console(page)
    page.goto("/admin/users")
    expect(page.locator("thead")).to_be_hidden()  # each user is a card on a phone
    expect(page.locator("tr", has_text=ADMIN_EMAIL).get_by_text("Last sign-in:")).to_be_visible()

    page.get_by_label("Email", exact=True).fill("new@example.com")
    page.get_by_role("button", name="Create account").click()

    expect(page.get_by_role("heading", name="Account created")).to_be_visible()
    expect(page.get_by_label("Temporary password")).to_have_value(re.compile(r"^[\w-]{16}$"))
    page.get_by_role("link", name="Back to users").click()
    expect(page.locator("tr", has_text="new@example.com")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert problems == []
