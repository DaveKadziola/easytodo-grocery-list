"""The board list in Chromium: renaming and archiving a board from its ⋮ menu, restoring it and
deleting it for good from "Archived (n)", leaving a shared board from its menu, and menus that
fit a phone and stay readable in the light and dark theme."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from factories import create_board, create_user
from playwright.sync_api import expect

from e2e.conftest import PHONE, open_board_menu, watch_console
from e2e.test_responsive import AA, LOW_CONTRAST_TEXT, at_least_44px, too_small_targets
from easytodo.models import BoardRole

pytestmark = pytest.mark.e2e


def archived_a_day_ago() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


def test_owner_renames_and_archives_a_board_from_its_menu(logged_in_page, seed, user):
    page = logged_in_page
    seed(lambda db: create_board(db, user, "Groceries"))
    problems = watch_console(page)
    statuses: list[int] = []
    page.on("response", lambda response: statuses.append(response.status))
    page.goto("/boards")

    expect(page.get_by_label("Board name")).to_be_hidden()  # in the menu, not on the page
    card = open_board_menu(page, "Groceries")
    card.get_by_label("Board name").fill("Łosoś")
    card.get_by_role("button", name="Rename").click()
    expect(page.get_by_role("link", name=re.compile("Łosoś"))).to_be_visible()  # reloaded

    card = open_board_menu(page, "Łosoś")
    confirms: list[str] = []
    page.once("dialog", lambda d: (confirms.append(d.message), d.accept()))
    card.get_by_role("button", name="Archive", exact=True).click()

    expect(page.locator("#toasts")).to_contain_text("Board archived.")
    expect(page.get_by_text("You have no boards yet.")).to_be_visible()
    assert confirms == ["Archive “Łosoś”? Its members lose access until you restore it."]

    page.get_by_role("link", name="Archived (1)").click()
    page.get_by_role("button", name="Restore").click()
    expect(page.locator("#toasts")).to_contain_text("Board restored.")
    page.get_by_role("link", name="Back to boards").click()
    expect(page.get_by_role("link", name=re.compile("Łosoś"))).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("Archived"))).to_have_count(0)
    assert 403 not in statuses and 422 not in statuses
    assert problems == []


def test_archived_board_is_deleted_by_typing_its_name(logged_in_page, seed, user):  # Review Focus 3
    page = logged_in_page
    seed(lambda db: create_board(db, user, "Łosoś", archived_at=archived_a_day_ago()))
    problems = watch_console(page)
    page.goto("/boards")
    expect(page.get_by_text("You have no boards yet.")).to_be_visible()

    page.get_by_role("link", name="Archived (1)").click()
    prompts: list[str] = []
    page.once("dialog", lambda d: (prompts.append(d.message), d.accept("Łosoś")))
    page.get_by_role("button", name="Delete permanently").click()

    expect(page.get_by_text("No archived boards.")).to_be_visible()
    expect(page.locator("#toasts")).to_contain_text("Board deleted permanently.")
    assert prompts == ["Type “Łosoś” to delete this board and everything on it permanently."]
    assert problems == []


def test_viewer_leaves_a_shared_board_from_its_menu(logged_in_page, seed, user):
    page = logged_in_page

    async def make(db) -> None:
        olga = await create_user(db, "olga@example.com")
        await create_board(db, olga, "Club", {user: BoardRole.VIEWER})

    seed(make)
    problems = watch_console(page)
    page.goto("/boards")
    expect(page.get_by_text("Owner: olga@example.com")).to_be_visible()

    card = open_board_menu(page, "Club")
    expect(card.locator(".menu").get_by_role("button")).to_have_text(["Leave board"])
    expect(card.locator(".menu").get_by_role("link")).to_have_count(0)
    confirms: list[str] = []
    page.once("dialog", lambda d: (confirms.append(d.message), d.accept()))
    card.get_by_role("button", name="Leave board").click()

    expect(page.get_by_text("Nobody has shared a board with you yet.")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("Club"))).to_have_count(0)
    assert confirms == ["Leave “Club”?"]
    assert problems == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_board_menu_fits_a_phone_and_stays_readable(signed_in_page, seed, user, scheme):
    page = signed_in_page(color_scheme=scheme, **PHONE)

    async def make(db) -> None:
        olga = await create_user(db, "olga@example.com")
        eve = await create_user(db, "eve@example.com")
        await create_board(db, user, "Groceries", {eve: BoardRole.EDITOR})
        await create_board(db, olga, "Club with a rather long name", {user: BoardRole.EDITOR})
        await create_board(db, user, "Old list", archived_at=archived_a_day_ago())

    seed(make)
    page.goto("/boards")
    expect(page.get_by_text("Shared", exact=True)).to_be_visible()  # the pill on "Groceries"
    at_least_44px(page.locator("summary"))

    cards = []
    # The lower card first: an open menu covers the cards below it, and so their ⋮ buttons.
    for name in ("Club with a rather long name", "Groceries"):
        cards.append(open_board_menu(page, name))
        expect(cards[-1].locator(".menu")).to_be_visible()
        assert too_small_targets(page) == [], name
        assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth"), name
        assert page.evaluate(LOW_CONTRAST_TEXT, AA) == [], name
    expect(cards[0].locator(".menu")).to_be_hidden()  # opening the second closed the first


def test_the_whole_card_opens_its_board_but_the_menu_button_does_not(logged_in_page, seed, user):
    page = logged_in_page
    seed(lambda db: create_board(db, user, "Groceries"))
    page.goto("/boards")

    open_board_menu(page, "Groceries")
    expect(page.locator(".menu")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/boards$"))
    page.keyboard.press("Escape")
    box = page.locator("li.card").bounding_box()
    page.mouse.click(box["x"] + box["width"] - 4, box["y"] + box["height"] - 4)  # a corner

    expect(page).to_have_url(re.compile(r"/boards/\d+$"))


def test_a_tap_that_dismisses_a_menu_does_not_open_a_board(signed_in_page, seed, user):
    page = signed_in_page(**PHONE)

    async def make(db) -> None:
        await create_board(db, user, "Groceries")
        await create_board(db, user, "Weekend")

    seed(make)
    page.goto("/boards")
    groceries = page.get_by_role("link", name=re.compile("Groceries"))

    # The lower card's menu: it opens downwards, clear of the card above.
    weekend = open_board_menu(page, "Weekend")
    groceries.tap()  # outside the menu, on another card's link

    expect(weekend.locator(".menu")).to_be_hidden()
    page.wait_for_timeout(300)  # a navigation would take a moment
    expect(page).to_have_url(re.compile(r"/boards$"))

    # Switching from one menu to another stays a single tap on the second ⋮ button.
    open_board_menu(page, "Weekend")
    groceries_menu = page.locator("li.card").filter(has=page.get_by_label("Actions for Groceries"))
    groceries_menu.locator("summary").tap()
    expect(groceries_menu.locator(".menu")).to_be_visible()
    expect(weekend.locator(".menu")).to_be_hidden()
    page.keyboard.press("Escape")

    groceries.tap()  # with no menu open it opens the board
    expect(page).to_have_url(re.compile(r"/boards/\d+$"))

    # Enter on a link is never a dismissing tap: it follows the link even with a menu open.
    page.goto("/boards")
    open_board_menu(page, "Weekend")
    groceries.focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"/boards/\d+$"))


# The ring's contrast against the page (WCAG 1.4.11 asks 3:1 for the focus indicator) and where
# it is drawn: on the link's ::after, which covers the whole card, not on the link itself.
FOCUS_RING = r"""
(link) => {
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const rgb = (color) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3), (v) => v / 255);
  };
  const luminance = ([r, g, b]) => {
    const linear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  };
  const ring = getComputedStyle(link, "::after");
  const [hi, lo] = [
    luminance(rgb(ring.outlineColor)),
    luminance(rgb(getComputedStyle(document.body).backgroundColor)),
  ].sort((a, b) => b - a);
  return {
    linkOutline: getComputedStyle(link).outlineStyle,
    ringOutline: ring.outlineStyle,
    ringWidth: parseFloat(ring.outlineWidth),
    ringCoversCard: parseFloat(ring.width) === link.closest("li").clientWidth,
    contrast: (hi + 0.05) / (lo + 0.05),
  };
}
"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_keyboard_focus_rings_the_whole_card(signed_in_page, seed, user, scheme):
    page = signed_in_page(color_scheme=scheme)
    seed(lambda db: create_board(db, user, "Groceries"))
    page.goto("/boards")
    link = page.get_by_role("link", name=re.compile("Groceries"))

    for _ in range(10):  # Tab from the top of the page to the card's link
        page.keyboard.press("Tab")
        if link.evaluate("(el) => el === document.activeElement"):
            break
    expect(link).to_be_focused()

    ring = link.evaluate(FOCUS_RING)
    assert ring["linkOutline"] == "none" and ring["ringOutline"] == "solid", ring
    assert ring["ringWidth"] >= 2 and ring["ringCoversCard"], ring
    assert ring["contrast"] >= 3, ring
