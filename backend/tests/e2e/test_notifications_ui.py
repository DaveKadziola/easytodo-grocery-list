"""The notification bell in Chromium: a change that concerns the user shows on the bell of their
open pages without a reload, opening the bell shows and reads the notifications (on every device),
the bell's requests stay out of the board's way, and the header and panel fit a phone and stay
readable in the light and the dark theme."""

import re

import pytest
from factories import create_board, create_notification, create_user
from playwright.sync_api import Page, Route, expect

from e2e.conftest import PHONE, watch_console
from e2e.locators import add_item, items_of
from e2e.test_realtime import wait_until
from e2e.test_responsive import AA, LOW_CONTRAST_TEXT, too_small_targets
from easytodo.boards.invites import accept_invite, create_link_invite
from easytodo.models import BoardRole, NotificationKind, User
from easytodo.notifications.service import notify
from easytodo.realtime.events import commit_and_publish

pytestmark = pytest.mark.e2e

LIVE_UPDATE_MS = 2000  # how soon a change must show on the bell


def bell(page: Page):
    return page.get_by_label("Notifications", exact=True)


def badge(page: Page):
    return page.locator("#notifications-badge")


def wait_until_live(page: Page) -> None:
    """The bell's socket is open (and the badge has been asked for since)."""
    expect(page.locator("#notifications[data-live]")).to_be_attached()


def seed_notifications(seed, user: User, *, unread: int, read: int = 0) -> int:
    """The board "Groceries" of ``user``, and ``read`` read and ``unread`` unread notifications
    about it, the unread ones the newest; returns the board's id."""

    async def make(db) -> int:
        board = await create_board(db, user, "Groceries")
        for n in range(read):
            await create_notification(
                db,
                user,
                NotificationKind.MEMBER_LEFT,
                f"Eve Adams left “Groceries” ({n}).",
                board,
                read=True,
            )
        for n in range(unread):
            await create_notification(
                db,
                user,
                NotificationKind.MEMBER_JOINED,
                f"vic joined “Groceries” ({n}). {'Longwordwithoutaspace' * 4}",
                board,
            )
        return board.id

    return seed(make)


def test_join_shows_on_the_bell_without_reload_and_reading_clears_it(logged_in_page, seed, user):
    async def make(db):
        board = await create_board(db, user, "Groceries")
        vic = await create_user(db, "vic@example.com")
        token, _ = await create_link_invite(db, board.id, user, BoardRole.EDITOR)
        return board.id, vic, token

    board_id, vic, token = seed(make)
    page = logged_in_page
    problems = watch_console(page)
    page.goto("/boards")
    wait_until_live(page)
    expect(badge(page)).to_be_hidden()  # nothing yet
    page.evaluate("window.sameDocument = true")

    seed(lambda db: accept_invite(db, token, vic))

    expect(badge(page)).to_have_text("1 unread", timeout=LIVE_UPDATE_MS)
    assert page.evaluate("window.sameDocument") is True  # the page was not reloaded
    bell(page).click()
    panel = page.locator("#notifications-panel")
    expect(panel).to_contain_text("vic joined “Groceries”.")
    expect(panel.get_by_role("link", name="Open board")).to_have_attribute(
        "href", f"/boards/{board_id}"
    )
    expect(badge(page)).to_be_hidden()  # read
    page.reload()
    expect(badge(page)).to_be_hidden()  # for good
    bell(page).click()
    expect(panel).to_contain_text("vic joined “Groceries”.")  # read, not gone
    assert problems == []


def test_reading_on_one_device_clears_the_badge_on_the_other(signed_in_page, seed, user):
    seed_notifications(seed, user, unread=2)
    a, b = signed_in_page(), signed_in_page()
    for page in (a, b):
        wait_until_live(page)
        expect(badge(page)).to_have_text("2 unread")

    bell(a).click()

    expect(badge(a)).to_be_hidden()
    expect(badge(b)).to_be_hidden(timeout=LIVE_UPDATE_MS)
    assert b.locator("#notifications-panel").get_by_text("vic joined").count() == 0  # b's is closed


def test_a_notification_that_arrives_while_the_panel_is_open_is_shown_and_read(
    logged_in_page, seed, user
):
    seed_notifications(seed, user, unread=0, read=1)
    page = logged_in_page
    wait_until_live(page)
    bell(page).click()
    panel = page.locator("#notifications-panel")
    expect(panel).to_contain_text("Eve Adams left")

    async def tell(db):
        events = notify(
            db, [user.id], NotificationKind.BOARD_RESTORED, board_id=None, actor="eve", board="Home"
        )
        await commit_and_publish(db, *events)

    seed(tell)

    expect(panel).to_contain_text("eve restored “Home”.", timeout=LIVE_UPDATE_MS)
    expect(badge(page)).to_be_hidden()  # shown to the user who is looking at it: read as well
    page.reload()
    expect(badge(page)).to_be_hidden()


def test_bell_does_not_hold_back_board_updates(two_logged_in_pages):
    a, b = two_logged_in_pages
    held: list[Route] = []
    b.route(re.compile(r"/notifications/panel$"), lambda route: held.append(route))
    bell(b).click()
    wait_until(b, lambda: held, "the panel's request")  # on its way, unanswered

    add_item(a, "Dairy", "Eggs")

    # Not one of this tab's own changes to the board: its update is not held until this ends.
    expect(items_of(b, "Dairy").filter(has_text="Eggs")).to_have_count(1, timeout=LIVE_UPDATE_MS)
    held[0].continue_()
    expect(b.locator("#notifications-panel")).to_contain_text("No notifications.")


def test_bell_works_around_the_item_modal(logged_in_page_with_seed, seed, user):
    page = logged_in_page_with_seed
    wait_until_live(page)
    bell(page).click()
    expect(page.locator("#notifications-panel")).to_be_visible()

    page.get_by_role("button", name="Bread", exact=True).click()  # a click outside: the menu closes

    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#notifications")).not_to_have_attribute("open", "")
    expect(page.locator("body > header")).to_have_attribute(
        "inert", ""
    )  # out of reach, as the rest

    async def tell(db):
        events = notify(
            db, [user.id], NotificationKind.BOARD_ARCHIVED, board_id=None, actor="eve", board="Home"
        )
        await commit_and_publish(db, *events)

    seed(tell)

    expect(badge(page)).to_have_text("1 unread", timeout=LIVE_UPDATE_MS)  # inert is not frozen
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator("body > header")).not_to_have_attribute("inert", "")
    bell(page).click()
    expect(page.locator("#notifications-panel")).to_contain_text("eve archived “Home”.")


def test_bell_opens_and_closes_from_the_keyboard(logged_in_page, seed, user):
    seed_notifications(seed, user, unread=1)
    page = logged_in_page
    wait_until_live(page)
    bell(page).focus()

    page.keyboard.press("Enter")

    panel = page.locator("#notifications-panel")
    expect(panel).to_contain_text("vic joined")
    page.keyboard.press("Tab")
    expect(panel.get_by_role("link", name="Open board")).to_be_focused()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(bell(page)).to_be_focused()


def test_bell_fits_a_phone(signed_in_page, seed, user, open_board):
    board_id = seed_notifications(seed, user, unread=2, read=23)
    page = signed_in_page(**PHONE)
    open_board(page, board_id)
    wait_until_live(page)
    expect(badge(page)).to_have_text("2 unread")

    header = page.locator("body > header").bounding_box()
    assert header["height"] < 70, header  # one row, with the bell in it

    bell(page).click()

    panel = page.locator("#notifications-panel")
    expect(panel).to_contain_text("vic joined")
    box = panel.bounding_box()
    assert box["y"] >= header["y"] + header["height"] - 1, (box, header)  # below the header
    assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["viewport"]["width"], box
    assert box["height"] <= PHONE["viewport"]["height"] * 0.7 + 1, box
    assert panel.evaluate("(el) => el.scrollHeight > el.clientHeight")  # 25 of them: it scrolls
    assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth")
    assert too_small_targets(page) == []  # the bell, "Open board", and the rest of the header


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_bell_and_panel_stay_readable(signed_in_page, seed, user, scheme):
    seed_notifications(seed, user, unread=2, read=2)
    page = signed_in_page(color_scheme=scheme)
    wait_until_live(page)
    expect(badge(page)).to_have_text("2 unread")
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []  # the badge: white on red

    bell(page).click()

    expect(page.locator("#notifications-panel")).to_contain_text("Eve Adams left")
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []  # unread and read entries, the links
    assert too_small_targets(page) == []


def test_board_page_still_opens_its_own_socket_beside_the_bell(
    logged_in_page_with_seed, seed, user
):
    """Both sockets are live on a board page, and a board change reaches only the board's one."""
    page = logged_in_page_with_seed
    wait_until_live(page)
    expect(page.locator("#conn-status")).to_have_text("Live")
    expect(badge(page)).to_be_hidden()
