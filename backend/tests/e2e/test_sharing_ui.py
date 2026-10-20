"""Sharing a board in Chromium: the owner creates an invite link and copies it, a newcomer signs
up from the link and joins, and the owner changes the newcomer's role and removes them, with no
console errors or CSP violations on any of these pages."""

import re

import pytest
from factories import DEFAULT_PASSWORD, create_board
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def watch_console(page: Page) -> list[str]:
    """Every console error or warning and every uncaught exception on ``page`` from now on."""
    problems: list[str] = []
    page.on(
        "console",
        lambda msg: (
            problems.append(f"{msg.type}: {msg.text}") if msg.type in ("error", "warning") else None
        ),
    )
    page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
    return problems


def test_invite_link_round_trip(browser, live_server, logged_in_page, seed, user):
    board_id = seed(lambda db: create_board(db, user, "Groceries")).id
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    problems = watch_console(page)
    members_url = re.compile(rf"/boards/{board_id}/members$")

    page.goto(f"/boards/{board_id}")
    page.get_by_role("link", name="Members").click()
    expect(page).to_have_url(members_url)
    page.get_by_label("Role", exact=True).select_option("viewer")
    page.get_by_role("button", name="Invite").click()
    link_field = page.get_by_label("Invite link")
    expect(link_field).to_have_value(re.compile(rf"^{live_server.url}/invite/[\w-]+$"))
    link = link_field.input_value()
    page.get_by_role("button", name="Copy").click()
    expect(page.locator("#toasts")).to_have_text("Copied.")
    assert page.evaluate("navigator.clipboard.readText()") == link
    expect(page).to_have_url(members_url)  # swapped in place: a reload no longer shows the link
    page.reload()
    expect(page.get_by_label("Invite link")).to_have_count(0)
    expect(page.get_by_text("Invite link", exact=True)).to_be_visible()  # listed as pending

    newcomer = browser.new_context(base_url=live_server.url)
    other = newcomer.new_page()
    problems += watch_console(other)
    other.goto(link)
    expect(other).to_have_url(re.compile(r"/login\?next="))
    other.get_by_role("link", name="Create an account").click()
    other.get_by_label("Email").fill("newbie@example.com")
    other.get_by_label("Password").fill(DEFAULT_PASSWORD)
    other.get_by_role("button", name="Create account").click()
    expect(other).to_have_url(link)
    expect(other.get_by_text("dave@example.com invited you to the board")).to_be_visible()
    other.get_by_role("button", name="Join board").click()
    expect(other).to_have_url(re.compile(rf"/boards/{board_id}$"))
    expect(other.get_by_role("heading", name="Groceries")).to_be_visible()
    newcomer.close()

    page.reload()
    role = page.get_by_label("Role of newbie@example.com")
    expect(role).to_have_value("viewer")
    with page.expect_response(re.compile(r"/members/\d+$")) as changed:
        role.select_option("editor")
    assert changed.value.status == 200
    expect(page.get_by_label("Role of newbie@example.com")).to_have_value("editor")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Remove").click()
    expect(page.get_by_text("newbie@example.com")).to_have_count(0)

    assert problems == []
