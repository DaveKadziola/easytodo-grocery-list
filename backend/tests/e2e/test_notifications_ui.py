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
from easytodo.notifications.service import notify, unread_count
from easytodo.realtime.events import commit_and_publish

pytestmark = pytest.mark.e2e

LIVE_UPDATE_MS = 2000  # how soon a change must show on the bell


def bell(page: Page):
    return page.get_by_label("Notifications", exact=True)


def badge(page: Page):
    return page.locator("#notifications-badge")


NO_BADGE_HTML = '<span id="notifications-badge" hidden></span>'


def wait_until_live(page: Page) -> None:
    """The bell's socket is open and the bell is up to date: the badge was asked for after it
    opened and the answer is in, so none of the bell's requests is out."""
    expect(page.locator("#notifications[data-live]")).to_be_attached()


SLOW_DOWN = """
(() => {
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.lateByTest = /\\/notifications\\/(badge|panel|read)$/.test(new URL(url, location).pathname);
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    if (this.lateByTest) setTimeout(() => send.apply(this, args), %d);
    else send.apply(this, args);
  };
})();
"""


def slow_down(page: Page, ms: int = 200) -> None:
    """Every request of the bell, on every page this page loads from now on, goes out ``ms`` late:
    a test that passes only when the round trips are quick does not pass with this."""
    page.add_init_script(SLOW_DOWN % ms)


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
    assert seed(lambda db: unread_count(db, user)) == 0
    page.reload()
    wait_until_live(page)  # the badge was asked for again and its answer is in
    expect(badge(page)).to_be_hidden()  # for good
    bell(page).click()
    expect(panel).to_contain_text("vic joined “Groceries”.")  # read, not gone
    assert problems == []


def test_reading_on_one_device_clears_the_badge_on_the_other(signed_in_page, seed, user):
    seed_notifications(seed, user, unread=2)
    a, b = signed_in_page(), signed_in_page()
    for page in (a, b):
        slow_down(page)
        page.reload()
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
    slow_down(page)
    page.reload()
    wait_until_live(page)
    bell(page).click()
    panel = page.locator("#notifications-panel")
    expect(panel).to_contain_text("Eve Adams left")

    async def tell(db):
        events = notify(
            db, [user.id], NotificationKind.BOARD_RESTORED, board_id=None, actor="eve", board="Home"
        )
        await commit_and_publish(db, *events)

    with page.expect_response(re.compile(r"/notifications/read$"), timeout=LIVE_UPDATE_MS * 2):
        seed(tell)

    expect(panel).to_contain_text("eve restored “Home”.", timeout=LIVE_UPDATE_MS)
    expect(badge(page)).to_be_hidden()  # shown to the user who is looking at it: read as well
    assert seed(lambda db: unread_count(db, user)) == 0  # read for real, not only out of sight
    page.reload()
    wait_until_live(page)  # the badge was asked for again and its answer is in
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


def test_bell_works_around_the_item_modal(logged_in_page, seeded_board, open_board, seed, user):
    page = logged_in_page
    slow_down(page)
    open_board(page, seeded_board)
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
    slow_down(page)
    page.reload()
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


def test_board_page_still_opens_its_own_socket_beside_the_bell(two_logged_in_pages):
    """Both sockets are live on a board page, and a board change reaches only the board's one."""
    a, b = two_logged_in_pages
    wait_until_live(b)
    expect(b.locator("#conn-status")).to_have_text("Live")
    expect(badge(b)).to_be_hidden()
    asked: list[str] = []
    b.on("request", lambda request: asked.append(request.url))

    add_item(a, "Dairy", "Eggs")

    expect(items_of(b, "Dairy").filter(has_text="Eggs")).to_have_count(1, timeout=LIVE_UPDATE_MS)
    b.wait_for_timeout(300)  # long enough for a request that is (wrongly) sent for it
    assert [url for url in asked if "/notifications/" in url] == []  # the bell was not asked
    expect(badge(b)).to_be_hidden()


def test_opening_the_bell_while_one_of_its_requests_is_out_still_shows_and_reads(
    logged_in_page, seed, user
):
    """A badge refresh is on its way (as after a push or a return to the tab) when the bell opens:
    the panel is fetched all the same, and read only after it is on the page."""
    seed_notifications(seed, user, unread=2)
    page = logged_in_page
    page.reload()  # the rows were added without a push
    wait_until_live(page)
    expect(badge(page)).to_have_text("2 unread")
    held: list[Route] = []
    page.route(
        re.compile(r"/notifications/badge$"),
        lambda route: route.continue_() if held else held.append(route),
    )
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # back on the tab
    wait_until(page, lambda: held, "the badge refresh")

    bell(page).click()  # while that request is out

    held[0].continue_()
    expect(page.locator("#notifications-panel")).to_contain_text("vic joined")
    expect(badge(page)).to_be_hidden()  # read, and this tab's badge says so
    assert seed(lambda db: unread_count(db, user)) == 0


def test_a_badge_that_loads_itself_does_not_take_the_bells_answer_away(logged_in_page, seed, user):
    """The badge asks for itself when the page loads, and the bell asks for it when its socket
    opens: whichever is answered second must still show, though the first replaced the element."""
    seed_notifications(seed, user, unread=2)
    page = logged_in_page
    held: list[Route] = []
    page.route(re.compile(r"/notifications/badge$"), lambda route: held.append(route))

    page.reload()
    wait_until(page, lambda: len(held) == 2, "the badge's own request and the bell's")
    # The page's own request is answered first, and with an old answer; the bell's, with the real
    # one, then belongs on the element that replaced the one it was asked for.
    held[0].fulfill(body=NO_BADGE_HTML, content_type="text/html")
    expect(badge(page)).to_be_hidden()
    held[1].continue_()

    expect(badge(page)).to_have_text("2 unread")


def test_refreshes_that_come_while_one_is_out_are_served_by_one_more(logged_in_page, seed, user):
    """A burst of reasons to look again (back on the tab, back online, pushes) while the badge is
    being fetched costs one more request, not one for each."""
    seed_notifications(seed, user, unread=2)
    page = logged_in_page
    page.reload()
    wait_until_live(page)
    expect(badge(page)).to_have_text("2 unread")
    asked: list[str] = []
    held: list[Route] = []

    def hold_the_first(route: Route) -> None:
        asked.append(route.request.url)
        if held:
            route.continue_()
        else:
            held.append(route)

    page.route(re.compile(r"/notifications/badge$"), hold_the_first)

    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    wait_until(page, lambda: held, "the first refresh")
    page.evaluate(
        """() => {
            for (let n = 0; n < 5; n++) document.dispatchEvent(new Event('visibilitychange'));
            window.dispatchEvent(new Event('online'));
        }"""
    )
    page.wait_for_timeout(300)
    assert len(asked) == 1  # still the one that is out

    held[0].continue_()

    wait_until(page, lambda: len(asked) >= 2, "the refresh that the burst asked for")
    page.wait_for_timeout(500)  # long enough for more of them, were there more
    assert len(asked) == 2
    expect(badge(page)).to_have_text("2 unread")


def test_a_bell_that_was_refused_stays_down(signed_in_page):
    """Signed out elsewhere: the socket is closed with 4401, and coming back to the tab or being
    back online does not try it again (every try would be refused). On the board list: a board
    page would leave for the login page at once."""
    a, b = signed_in_page(), signed_in_page()
    wait_until_live(b)  # the page that signing in opened has its own socket by now: leave it out
    sockets = []
    b.on("websocket", lambda ws: sockets.append(ws) if "/ws/user" in ws.url else None)
    b.reload()
    wait_until_live(b)
    assert len(sockets) == 1

    a.goto("/account")
    a.get_by_role("button", name="Sign out other devices").click()
    wait_until(b, lambda: sockets[0].is_closed(), "the bell's socket closing")
    expect(b.locator("#notifications[data-live]")).to_have_count(0)

    b.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    b.evaluate("window.dispatchEvent(new Event('online'))")
    b.wait_for_timeout(500)  # long enough for a socket that is (wrongly) opened again

    assert len(sockets) == 1
