"""The account pages in Chromium: the flash toast after a password change shows exactly once, and
the recovery codes of two-factor authentication are saved as a .txt file or copied."""

import re
from pathlib import Path

import pyotp
import pytest
from factories import DEFAULT_PASSWORD
from playwright.sync_api import Page, expect

from e2e.conftest import watch_console
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


def turn_on_2fa(page: Page) -> list[str]:
    """Turn two-factor authentication on from the setup page; returns the recovery codes it shows."""
    page.goto("/account/2fa/setup")
    secret = page.locator("#totp-secret").inner_text()
    page.get_by_label("Current password").fill(DEFAULT_PASSWORD)
    page.get_by_label("Code from the app").fill(pyotp.TOTP(secret).now())
    page.get_by_role("button", name="Turn on two-factor authentication").click()
    expect(page.get_by_role("heading", name="Recovery codes")).to_be_visible()
    return page.locator("[data-recovery-code]").all_inner_texts()


def test_recovery_codes_download_as_txt(logged_in_page):
    page = logged_in_page
    problems = watch_console(page)
    codes = turn_on_2fa(page)

    with page.expect_download() as info:
        page.get_by_role("button", name="Save as .txt").click()

    assert info.value.suggested_filename == "easytodo-recovery-codes.txt"
    text = Path(info.value.path()).read_text(encoding="utf-8")
    assert text.startswith("EasyToDo recovery codes\nAccount: dave@example.com\nCreated: ")
    assert len(codes) == 10 and all(code in text for code in codes)
    assert text == page.locator("#recovery-codes-text").input_value()
    expect(page.locator("#toasts > *")).to_have_count(0)
    assert problems == []


def test_recovery_codes_copy(live_server, logged_in_page):
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    problems = watch_console(page)
    old = turn_on_2fa(page)
    # The page that new codes come on is the other place that shows them.
    page.goto("/account")
    page.locator("#recovery_password").fill(DEFAULT_PASSWORD)
    page.get_by_role("button", name="Get new recovery codes").click()
    expect(page.get_by_role("heading", name="Recovery codes")).to_be_visible()
    codes = page.locator("[data-recovery-code]").all_inner_texts()

    copy = page.get_by_role("button", name="Copy")
    copy.click()

    expect(page.locator("#toasts")).to_have_text("Copied.")
    expect(copy).to_be_focused()  # not the hidden field, which is aria-hidden
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == page.locator("#recovery-codes-text").input_value()
    assert (
        len(codes) == 10 and all(c in copied for c in codes) and not any(c in copied for c in old)
    )
    assert problems == []
