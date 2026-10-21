"""The account pages in Chromium: the flash toast after a password change shows exactly once."""

import re

import pytest
from factories import DEFAULT_PASSWORD
from playwright.sync_api import expect

from easytodo.web import FLASH_COOKIE

pytestmark = pytest.mark.e2e


def test_password_changed_toast_shows_once(logged_in_page):
    page = logged_in_page
    page.goto("/account/password")

    page.get_by_label("Current password").fill(DEFAULT_PASSWORD)
    page.get_by_label("New password", exact=True).fill("a-brand-new-passphrase")
    page.get_by_label("Repeat new password").fill("a-brand-new-passphrase")
    page.get_by_role("button", name="Change password").click()

    expect(page).to_have_url(re.compile(r"/account$"))
    expect(page.locator("#toasts > *")).to_have_count(1)
    expect(page.locator("#toasts")).to_have_text("Password changed.")
    assert FLASH_COOKIE not in page.evaluate("document.cookie")

    page.reload()
    expect(page.get_by_role("heading", name="Account", exact=True)).to_be_visible()
    expect(page.locator("#toasts > *")).to_have_count(0)
