"""Sharing a board in Chromium: the owner creates an invite link and copies it, a newcomer signs
up from the link and joins (also while registration is closed), and the owner changes the
newcomer's role and removes them, with no console errors or CSP violations on any of these
pages."""

import re

import pytest
from factories import DEFAULT_PASSWORD, create_board
from playwright.sync_api import Browser, Page, expect

from e2e.conftest import open_board_menu, watch_console
from easytodo.config import Settings

pytestmark = pytest.mark.e2e


def create_invite_link(page: Page, base_url: str, board_id: int) -> str:
    """From the ⋮ menu of the board "Groceries" on the board list, go to its members page and
    create a link invite as a viewer; returns the link the page shows (once)."""
    page.goto("/boards")
    open_board_menu(page, "Groceries").get_by_role("link", name="Invite", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/boards/{board_id}/members$"))
    page.get_by_label("Role", exact=True).select_option("viewer")
    page.get_by_role("button", name="Invite").click()
    link_field = page.get_by_label("Invite link")
    expect(link_field).to_have_value(re.compile(rf"^{base_url}/invite/[\w-]+$"))
    return link_field.input_value()


def sign_up_and_join(browser: Browser, base_url: str, link: str, board_id: int) -> list[str]:
    """A newcomer, in a browser of their own, opens ``link`` signed out, creates an account from
    the sign-in page it leads to, and joins the board. Returns the console problems seen."""
    newcomer = browser.new_context(base_url=base_url)
    other = newcomer.new_page()
    problems = watch_console(other)
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
    return problems


def test_invite_link_round_trip(browser, live_server, logged_in_page, seed, user):
    board_id = seed(lambda db: create_board(db, user, "Groceries")).id
    page = logged_in_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    problems = watch_console(page)
    members_url = re.compile(rf"/boards/{board_id}/members$")

    link = create_invite_link(page, live_server.url, board_id)
    page.get_by_role("button", name="Copy").click()
    expect(page.locator("#toasts")).to_have_text("Copied.")
    assert page.evaluate("navigator.clipboard.readText()") == link
    expect(page).to_have_url(members_url)  # swapped in place: a reload no longer shows the link
    page.reload()
    expect(page.get_by_label("Invite link")).to_have_count(0)
    expect(page.get_by_text("Invite link", exact=True)).to_be_visible()  # listed as pending

    problems += sign_up_and_join(browser, live_server.url, link, board_id)

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


class TestRegistrationClosed:
    @pytest.fixture
    def settings(self, settings: Settings) -> Settings:
        """The live server's settings: EASYTODO_REGISTRATION_OPEN=false."""
        return settings.model_copy(update={"registration_open": False})

    def test_invite_link_still_lets_a_newcomer_sign_up_and_join(
        self, browser, live_server, logged_in_page, seed, user
    ):
        assert live_server.settings.registration_open is False
        board_id = seed(lambda db: create_board(db, user, "Groceries")).id
        problems = watch_console(logged_in_page)

        link = create_invite_link(logged_in_page, live_server.url, board_id)
        problems += sign_up_and_join(browser, live_server.url, link, board_id)

        logged_in_page.reload()
        expect(logged_in_page.get_by_label("Role of newbie@example.com")).to_have_value("viewer")
        assert problems == []
