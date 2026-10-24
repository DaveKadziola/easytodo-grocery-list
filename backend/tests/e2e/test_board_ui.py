"""The board page in Chromium: the phone layout, adding and ticking items, drag and drop (mouse
and touch), the item modal, the category menu, the "Archived boards" page and the board's
"Archived categories" page, a change that lost a race with another device, and no CSP
violations anywhere. (Renaming and archiving a board are tested on the board list, in
test_board_list_ui.py.)"""

import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import pytest
from factories import create_board, create_category, create_item, create_user
from helpers import NASTY
from playwright.sync_api import Locator, Page, Request, Route, expect
from sqlalchemy import func, update

from e2e import test_responsive
from e2e.conftest import PICKED_UP, open_board_menu, watch_console
from e2e.locators import TAP_TARGETS, add_item, category, item, items_of
from e2e.test_resync import expect_struck
from easytodo.models import Board, BoardMember, BoardRole, Category, Item

pytestmark = pytest.mark.e2e

# On a phone the bottom bar covers the foot of the screen: what a finger goes for is scrolled up.
SCROLL_CLEAR_OF_THE_BOTTOM_BAR = "(el) => el.scrollIntoView({ block: 'center' })"


def seed_board(
    seed,
    owner,
    categories: dict[str, list[str]],
    done: tuple[str, ...] = (),
    *,
    name: str = "Groceries",
    archived: bool = False,
) -> int:
    """A board ``name`` of ``owner`` (archived a day ago if ``archived``) with these categories (in
    this order) and items, the ones named in ``done`` ticked; returns the board's id."""

    async def make(db) -> int:
        archived_at = datetime.now(UTC) - timedelta(days=1) if archived else None
        board = await create_board(db, owner, name, archived_at=archived_at)
        for position, (category_name, items) in enumerate(categories.items()):
            category = await create_category(db, board, category_name, position)
            for item_name in items:
                await create_item(db, category, item_name, is_done=item_name in done)
        return board.id

    return seed(make)


def describe(seed, item_name: str, description: str) -> None:
    async def run(db) -> None:
        await db.execute(update(Item).where(Item.name == item_name).values(description=description))
        await db.commit()

    seed(run)


def sort_dairy(page: Page, order: str) -> None:
    """Pick ``order`` ("Z–A / newest first", ...) in the "Sort Dairy" menu and press "Apply"; returns
    once the category is back with its order marked as the user's own."""
    dairy = category(page, "Dairy")
    dairy.get_by_label("Sort Dairy").click()
    dairy.get_by_role("radio", name=order, exact=True).check()
    with page.expect_response(re.compile(r"/categories/\d+/sort$")) as saved:
        dairy.get_by_role("button", name="Apply", exact=True).click()
    assert saved.value.status == 200
    expect(dairy.get_by_label("Sort Dairy")).to_have_class(re.compile(r"\bsort-custom\b"))


def too_small_targets(page: Page) -> list[str]:
    return page.locator("#board-content").evaluate(
        """(board, selector) => Array.from(board.querySelectorAll(selector))
            .filter((el) => el.checkVisibility())
            .map((el) => [el.outerHTML.slice(0, 80), el.getBoundingClientRect()])
            .filter(([, box]) => box.width < 44 || box.height < 44)
            .map(([html, box]) => `${html} is ${box.width}x${box.height}`)""",
        TAP_TARGETS,
    )


def test_add_and_toggle_item_on_phone(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    board_id = seed_board(seed, user, {"Dairy": ["Yogurt"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    dairy = category(page, "Dairy")
    new_item = dairy.get_by_placeholder("Add item")

    new_item.fill("Milk")
    dairy.get_by_role("button", name="Add", exact=True).tap()

    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk"), re.compile("Yogurt")])
    expect(new_item).to_have_value("")
    expect(new_item).to_be_focused()

    dairy.get_by_role("checkbox", name="Milk").tap()

    expect(dairy.get_by_role("checkbox", name="Milk")).to_be_checked()
    expect(item(page, "Milk")).to_have_class(re.compile(r"\bline-through\b"))
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Yogurt"), re.compile("Milk")])
    assert too_small_targets(page) == []


def test_shared_board_shows_who_added_each_item(logged_in_page, seed, user, open_board):
    page = logged_in_page
    problems = watch_console(page)

    async def make(db) -> tuple[int, int]:
        anna = await create_user(db, "anna@example.com", display_name="Anna Kowalska")
        shared = await create_board(db, user, "Groceries", {anna: BoardRole.EDITOR})
        dairy = await create_category(db, shared, "Dairy")
        await create_item(db, dairy, "Milk", created_by=user)
        await create_item(db, dairy, "Bread", created_by=anna)
        await create_item(db, dairy, "Eggs")  # its author's account is gone
        solo = await create_board(db, user, "Solo")
        await create_item(db, await create_category(db, solo, "Dairy"), "Cheese", created_by=user)
        return shared.id, solo.id

    shared_id, solo_id = seed(make)

    open_board(page, shared_id)

    milk, bread = item(page, "Milk").locator(".avatar"), item(page, "Bread").locator(".avatar")
    expect(milk).to_have_text("D")
    expect(milk).to_have_attribute("title", "dave")
    expect(bread).to_have_text("AK")
    expect(bread).to_have_attribute("title", "Anna Kowalska")
    expect(item(page, "Eggs").locator(".avatar")).to_have_count(0)
    # The avatar sits beside the name, not in it: the checkbox is still named by the name alone.
    expect(page.get_by_role("checkbox", name="Milk", exact=True)).to_have_count(1)
    add_item(page, "Dairy", "Butter")
    expect(item(page, "Butter").locator(".avatar")).to_have_text("D")

    open_board(page, solo_id)

    expect(page.locator("#board-content li[data-item-id]")).to_have_count(1)
    expect(page.locator("#board-content .avatar")).to_have_count(0)
    assert problems == []


def test_drag_item_to_other_category(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk", "Yogurt"], "Bakery": ["Bread"]})
    open_board(page, board_id)

    bread = items_of(page, "Bakery").first
    with page.expect_response(re.compile(r"/items/\d+/move$")) as moved:
        # Into another list Sortable inserts only off the target's centre line, as a hand does.
        below_centre = {"x": 20, "y": bread.bounding_box()["height"] - 6}
        item(page, "Milk").drag_to(bread, target_position=below_centre)

    assert moved.value.status == 200
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    page.reload()
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Yogurt")])


def touch_drag(page: Page, source: Locator, target: Locator, landed: Locator | None) -> None:
    """A finger on ``source``, moved onto ``target`` and lifted. With ``landed`` it is a drag: the
    finger waits until something is picked up (Sortable's long press), and lifts when ``landed``
    shows the dragged element where the finger is. Without, it is a swipe, which scrolls."""
    target.evaluate(SCROLL_CLEAR_OF_THE_BOTTOM_BAR)
    start, end = source.bounding_box(), target.bounding_box()
    touch = page.context.new_cdp_session(page)

    def finger(kind: str, x: float | None = None, y: float | None = None) -> None:
        points = [] if x is None else [{"x": x, "y": y}]
        touch.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    x = start["x"] + start["width"] / 2
    finger("touchStart", x, start["y"] + 10)
    if landed is not None:
        expect(page.locator(PICKED_UP)).to_have_count(1)
    for step in range(1, 11):
        finger("touchMove", x, start["y"] + 10 + (end["y"] - start["y"]) * step / 10)
        page.wait_for_timeout(20)
    if landed is not None:
        expect(landed).to_have_count(1)  # Sortable looks under the finger every 50 ms
    else:
        expect(page.locator(PICKED_UP)).to_have_count(0)  # a swipe picks nothing up
    finger("touchEnd")


def test_long_press_drags_an_item_on_a_touch_screen(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    moves: list[str] = []
    page.on(
        "request", lambda request: moves.append(request.url) if "/move" in request.url else None
    )

    touch_drag(page, item(page, "Milk"), item(page, "Bread"), landed=None)  # a swipe scrolls
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk")])

    bakery = category(page, "Bakery")
    with page.expect_response(re.compile(r"/items/\d+/move$")) as moved:
        # A long press: the item is picked up, and lifted where it has landed in Bakery.
        touch_drag(
            page, item(page, "Milk"), item(page, "Bread"), landed=bakery.locator(f"li{PICKED_UP}")
        )

    assert moved.value.status == 200
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    assert len(moves) == 1  # only the long press saved a move


def test_no_csp_violations(browser, live_server, logged_in_page, seed, user, open_board):
    anonymous = browser.new_context(base_url=live_server.url)
    page = anonymous.new_page()
    problems = watch_console(page)
    for path in ("/register", "/login", "/password/forgot"):
        page.goto(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
    anonymous.close()

    board_id = seed_board(
        seed, user, {"Dairy": ["Milk", "Yogurt"], "Bakery": ["Bread"]}, done=("Milk",)
    )
    page = logged_in_page
    board_problems = watch_console(page)
    page.goto("/boards")
    menu = open_board_menu(page, "Groceries")  # as the owner: rename, invite and archive
    expect(menu.get_by_role("button", name="Archive", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    open_board(page, board_id)
    expect(page.get_by_label("Board actions")).to_have_count(0)  # not on the board any more
    # A drag, whose answer swaps the whole board in: htmx must not copy the dragged item's
    # empty style attribute (from SortableJS) over, which style-src 'self' would refuse.
    bread = items_of(page, "Bakery").first
    with page.expect_response(re.compile(r"/items/\d+/move$")):
        below_centre = {"x": 20, "y": bread.bounding_box()["height"] - 6}
        item(page, "Yogurt").drag_to(bread, target_position=below_centre)
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Yogurt")])
    page.get_by_role("button", name="Milk", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Name")).to_have_value("Milk")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    sort_dairy(page, "Z–A / newest first")  # the sort menu: its form and its answer, too
    category(page, "Dairy").get_by_label("Category actions").click()
    expect(category(page, "Dairy").get_by_role("button", name="Clear done (1)")).to_be_visible()
    seed_board(seed, user, {"Tools": ["Hammer"]}, name="Old list", archived=True)
    page.goto("/boards/archived")
    expect(page.get_by_role("heading", name="Old list")).to_be_visible()

    assert [p for p in problems if "Content Security Policy" in p or "Refused to" in p] == []
    assert problems == [] and board_problems == []


def test_archived_board_is_restored_from_the_archive_page(logged_in_page, seed, user):
    page = logged_in_page
    seed_board(seed, user, {"Dairy": ["Milk"]}, name="Weekend", archived=True)
    problems = watch_console(page)
    page.goto("/boards")
    expect(page.get_by_text("You have no boards yet.")).to_be_visible()

    page.goto("/boards/archived")
    page.get_by_role("button", name="Restore").click()

    expect(page.locator("#toasts")).to_contain_text("Board restored.")
    expect(page.get_by_text("No archived boards.")).to_be_visible()
    page.get_by_role("link", name="Back to boards").click()
    expect(page.get_by_role("link", name=re.compile("Weekend"))).to_be_visible()
    assert problems == []


def test_editing_only_the_name_keeps_the_description(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    describe(seed, "Milk", "2 litres, organic")
    open_board(page, board_id)

    page.get_by_role("button", name="Milk", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Name")).to_be_focused()
    expect(dialog.get_by_label("Description")).to_have_value("2 litres, organic")
    dialog.get_by_label("Name").fill("Oat milk")
    dialog.get_by_role("button", name="Save").click()

    expect(dialog).to_be_hidden()
    expect(item(page, "Oat milk")).to_contain_text("2 litres, organic")
    page.reload()
    expect(item(page, "Oat milk")).to_contain_text("2 litres, organic")


def test_item_modal_shows_names_with_quotes_and_html_literally(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {NASTY: [NASTY]})
    describe(seed, NASTY, NASTY)
    open_board(page, board_id)
    page.on("dialog", lambda dialog: pytest.fail(f"a script ran: {dialog.message}"))

    page.get_by_role("button", name=NASTY, exact=True).click()

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Name")).to_have_value(NASTY)
    expect(dialog.get_by_label("Description")).to_have_value(NASTY)
    expect(dialog.get_by_label("Category").locator("option")).to_have_text([NASTY])
    expect(page.get_by_role("heading", name=NASTY, exact=True)).to_be_visible()


def test_viewer_reads_an_item_in_a_read_only_dialog(signed_in_as, seed, user, open_board):
    async def make(db) -> int:
        vic = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", {vic: BoardRole.VIEWER})
        dairy = await create_category(db, board, "Dairy")
        await create_item(db, dairy, "Milk", created_by=user, description="2 litres")
        return board.id

    board_id = seed(make)
    page = signed_in_as("vic@example.com")
    problems = watch_console(page)
    open_board(page, board_id)
    opener = page.get_by_role("button", name="Milk", exact=True)

    opener.click()

    dialog = page.get_by_role("dialog", name="Milk")
    expect(dialog).to_contain_text("2 litres")
    expect(dialog.locator("#item-details")).to_contain_text("Modified")  # loaded
    expect(dialog.locator("#item-details")).to_contain_text("Added by dave")
    expect(dialog.locator("input, textarea, select, form")).to_have_count(0)
    expect(dialog.get_by_role("checkbox")).to_have_count(0)
    expect(page.locator("#board-content")).to_have_attribute("inert", "")
    close = dialog.get_by_role("button", name="Close")
    expect(close).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()  # the only control: Tab stays on it
    page.keyboard.press("Shift+Tab")
    expect(close).to_be_focused()

    # Text cannot take the focus, so a click on it leaves it on the dialog's panel: Shift+Tab
    # still goes round to "Close", and Escape is still heard.
    dialog.get_by_text("2 litres").click()
    page.keyboard.press("Shift+Tab")
    expect(close).to_be_focused()
    dialog.get_by_text("2 litres").click()
    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
    assert problems == []


def test_item_modal_moves_and_deletes_items(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Butter", "Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    dialog = page.get_by_role("dialog")

    page.get_by_role("button", name="Milk", exact=True).click()
    expect(dialog.get_by_label("Category").locator("option")).to_have_text(["Dairy", "Bakery"])
    expect(dialog.get_by_label("Category")).to_have_value(
        category(page, "Dairy").get_attribute("data-category-id")
    )
    dialog.get_by_label("Category").select_option(label="Bakery")
    dialog.get_by_role("button", name="Save").click()
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])

    page.get_by_role("button", name="Butter", exact=True).click()
    page.once("dialog", lambda question: question.dismiss())  # "Delete this item?" No
    dialog.get_by_role("button", name="Delete item").click()
    expect(dialog).to_be_visible()
    page.wait_for_timeout(300)
    expect(items_of(page, "Dairy")).to_have_count(1)  # kept: a single tap deletes nothing

    page.once("dialog", lambda question: question.accept())
    dialog.get_by_role("button", name="Delete item").click()
    expect(dialog).to_be_hidden()
    expect(items_of(page, "Dairy")).to_have_count(0)

    page.reload()
    expect(items_of(page, "Dairy")).to_have_count(0)
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_delete_item_then_undo(two_logged_in_pages):
    a, b = two_logged_in_pages
    problems = {"a": watch_console(a), "b": watch_console(b)}

    a.get_by_role("button", name="Milk", exact=True).click()
    a.once("dialog", lambda question: question.accept())  # "Delete this item?"
    a.get_by_role("dialog").get_by_role("button", name="Delete item").click()

    expect(a.locator("#toasts")).to_have_text("Item deleted.Undo")
    undo = a.get_by_role("button", name="Undo")
    box = undo.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44, box
    expect(items_of(a, "Dairy")).to_have_count(0)
    expect(items_of(b, "Dairy")).to_have_count(0, timeout=2000)

    undo.click()

    expect(a.locator("#toasts > *")).to_have_count(0)
    expect(items_of(a, "Dairy")).to_have_text([re.compile("Milk")])
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")], timeout=2000)
    a.reload()
    expect(items_of(a, "Dairy")).to_have_text([re.compile("Milk")])
    assert problems == {"a": [], "b": []}


def test_undo_is_reachable_from_the_keyboard(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    problems = watch_console(page)
    dialog = page.get_by_role("dialog")

    page.get_by_role("button", name="Milk", exact=True).focus()
    page.keyboard.press("Enter")
    expect(dialog.get_by_label("Name")).to_be_focused()
    for control in (
        dialog.get_by_label("Description"),
        dialog.get_by_label("Category"),
        dialog.get_by_role("button", name="Delete item"),
    ):
        page.keyboard.press("Tab")
        expect(control).to_be_focused()
    page.once("dialog", lambda question: question.accept())  # "Delete this item?"
    page.keyboard.press("Enter")

    expect(items_of(page, "Dairy")).to_have_count(0)
    expect(page.get_by_role("button", name="Undo")).to_be_focused()  # not lost on the page
    page.keyboard.press("Enter")

    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk")])
    expect(page.locator("#toasts > *")).to_have_count(0)
    assert problems == []


def test_new_category_dialog_adds_at_the_bottom_or_on_top(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {})
    open_board(page, board_id)
    problems = watch_console(page)
    expect(page.get_by_text("Add your first category")).to_be_visible()
    opener = page.get_by_role("button", name="New category")
    dialog = page.get_by_role("dialog", name="New category")
    # "Category name" is also the label of the sections' rename fields (and of the move dialog's).
    name = dialog.get_by_label("Category name")
    bottom, top = dialog.get_by_label("At the bottom"), dialog.get_by_label("On top")

    for category_name, placement in (
        ("Dairy", bottom),
        ("Bakery", bottom),
        ("Fruit", top),
    ):
        opener.click()
        expect(dialog).to_be_visible()
        expect(page.locator("#board-content")).to_have_attribute("inert", "")
        expect(name).to_be_focused()
        expect(name).to_have_value("")  # what the last time left is gone
        expect(bottom).to_be_checked()
        name.fill(category_name)
        placement.check()
        name.press("Enter")

        expect(dialog).to_be_hidden()
        expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
        expect(category(page, category_name).get_by_placeholder("Add item")).to_be_focused()

    expect(page.get_by_text("Add your first category")).to_be_hidden()
    sections = page.locator("section[data-category-id] h2")
    expect(sections).to_have_text(["Fruit", "Dairy", "Bakery"])
    page.reload()
    expect(sections).to_have_text(["Fruit", "Dairy", "Bakery"])

    # Tab goes round the dialog, and the two radio buttons are one stop (the checked one).
    opener.click()
    expect(name).to_be_focused()
    for control in (
        bottom,
        dialog.get_by_role("button", name="Cancel"),
        dialog.get_by_role("button", name="Add category"),
        name,
    ):
        page.keyboard.press("Tab")
        expect(control).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Add category")).to_be_focused()

    # Escape, a tap beside the dialog and "Cancel" give the focus back to the button.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
    opener.click()
    expect(name).to_be_focused()
    page.mouse.click(x=5, y=5)
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    opener.click()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
    assert problems == []

    # A name that is taken: the toast says so, and the dialog stays with the name.
    opener.click()
    name.fill("dairy")
    name.press("Enter")
    expect(page.locator("#toasts")).to_contain_text("A category with this name already exists.")
    expect(dialog).to_be_visible()
    expect(name).to_have_value("dairy")
    expect(sections).to_have_text(["Fruit", "Dairy", "Bakery"])


def test_new_category_dialog_keeps_what_was_typed_until_the_category_is_added(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    opener = page.get_by_role("button", name="New category")
    dialog = page.get_by_role("dialog", name="New category")
    name = dialog.get_by_label("Category name")
    bottom, top = dialog.get_by_label("At the bottom"), dialog.get_by_label("On top")
    opener.click()
    name.fill("Bak")
    top.check()

    # A stray tap beside the dialog on a phone (Escape and "Cancel" too) must not cost the name.
    for close in (
        lambda: page.keyboard.press("Escape"),
        lambda: page.mouse.click(x=5, y=5),
        lambda: dialog.get_by_role("button", name="Cancel").click(),
    ):
        close()
        expect(dialog).to_be_hidden()
        opener.click()
        expect(dialog).to_be_visible()
        expect(name).to_be_focused()
        expect(name).to_have_value("Bak")
        expect(top).to_be_checked()

    name.fill("Bakery")
    name.press("Enter")
    expect(dialog).to_be_hidden()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery", "Dairy"])

    opener.click()  # an added category takes the name and the choice with it
    expect(name).to_have_value("")
    expect(bottom).to_be_checked()


def test_new_category_on_top_of_an_empty_board_goes_before_its_placeholder(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {})
    open_board(page, board_id)
    opener = page.get_by_role("button", name="New category")
    dialog = page.get_by_role("dialog", name="New category")
    sections = page.locator("section[data-category-id] h2")
    children = page.locator("#categories > *")

    for name in ("Fruit", "Nuts"):
        opener.click()
        dialog.get_by_label("Category name").fill(name)
        dialog.get_by_label("On top").check()
        dialog.get_by_label("Category name").press("Enter")
        expect(dialog).to_be_hidden()
        expect(category(page, name).get_by_placeholder("Add item")).to_be_focused()

    expect(sections).to_have_text(["Nuts", "Fruit"])
    expect(page.get_by_text("Add your first category")).to_be_hidden()
    expect(children.first).to_have_attribute("data-category-id", re.compile(r"^\d+$"))
    expect(children.last).to_have_text("Add your first category")  # the placeholder stays last
    first = page.locator("section[data-category-id]").first
    first.get_by_label("Category actions").click()
    expect(first.get_by_role("button", name="Move up")).to_be_hidden()  # it is the first
    expect(first.get_by_role("button", name="Move down")).to_be_visible()

    page.reload()
    expect(sections).to_have_text(["Nuts", "Fruit"])


@pytest.mark.parametrize(
    ("moved", "order"),
    [
        ("the new first one goes down", ["Dairy", "Fruit", "Bakery"]),
        ("the old first one goes up", ["Dairy", "Fruit", "Bakery"]),
        ("the last one is dragged to the top", ["Bakery", "Fruit", "Dairy"]),
    ],
)
def test_a_category_added_on_top_is_moved_like_any_other_without_a_reload(
    logged_in_page, seed, user, open_board, moved, order
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": [], "Bakery": []})
    open_board(page, board_id)
    page.get_by_role("button", name="New category").click()
    dialog = page.get_by_role("dialog", name="New category")
    dialog.get_by_label("Category name").fill("Fruit")
    dialog.get_by_label("On top").check()
    dialog.get_by_label("Category name").press("Enter")
    expect(dialog).to_be_hidden()
    expect(category_names(page)).to_have_text(["Fruit", "Dairy", "Bakery"])
    # What makes this a case of its own: the hidden placeholder is the list's first child, and the
    # new section was put before it, so "Fruit" is not next to "Dairy" in the page (no reload since).
    expect(page.locator("#categories > *").nth(1)).to_have_text("Add your first category")

    with page.expect_response(ORDER_SAVED) as saved:
        if moved == "the last one is dragged to the top":
            category(page, "Bakery").locator(".category-handle").drag_to(
                category(page, "Fruit").locator(".category-handle")
            )
        else:
            moving, button = (
                (category(page, "Fruit"), "Move down")
                if moved == "the new first one goes down"
                else (category(page, "Dairy"), "Move up")
            )
            moving.get_by_label("Category actions").click()
            moving.get_by_role("button", name=button).click()

    assert saved.value.status == 204
    expect(category_names(page)).to_have_text(order)
    page.reload()
    expect(category_names(page)).to_have_text(order)


@pytest.mark.parametrize("refusal", ["forbidden", "gone"])
def test_new_category_dialog_closes_when_the_board_refuses(
    signed_in_as, seed, user, open_board, refusal
):
    async def make(db) -> tuple[int, int]:
        vic = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", {vic: BoardRole.EDITOR})
        await create_category(db, board, "Dairy")
        return board.id, vic.id

    board_id, vic_id = seed(make)
    page = signed_in_as("vic@example.com")
    open_board(page, board_id)
    dialog = page.get_by_role("dialog", name="New category")
    page.get_by_role("button", name="New category").click()
    dialog.get_by_label("Category name").fill("Bakery")

    async def change_elsewhere(db) -> None:  # on another device, and this page was not told
        if refusal == "forbidden":
            await db.execute(
                update(BoardMember)
                .where(BoardMember.board_id == board_id, BoardMember.user_id == vic_id)
                .values(role=BoardRole.VIEWER)
            )
        else:
            await db.execute(
                update(Board).where(Board.id == board_id).values(archived_at=datetime.now(UTC))
            )
        await db.commit()

    seed(change_elsewhere)

    # A 404 asks for the board to be reloaded: the reload was held back by the open dialog, and
    # runs once the dialog has closed on its own (nothing here closes it but the refusal).
    if refusal == "gone":
        reload_wait = page.expect_request(re.compile(rf"/boards/{board_id}/content$"))
    else:
        reload_wait = nullcontext()
    with reload_wait:
        dialog.get_by_label("Category name").press("Enter")
        expect(dialog).to_be_hidden()
        expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
    expect(page.locator("#toasts")).to_contain_text(
        "You don't have permission to do that." if refusal == "forbidden" else "Board not found."
    )


def test_category_menu_renames_clears_done_and_archives(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(
        seed, user, {"Dairy": ["Milk", "Yogurt"], "Bakery": ["Bread"]}, done=("Milk",)
    )
    open_board(page, board_id)

    category(page, "Dairy").get_by_label("Category actions").click()
    category(page, "Dairy").get_by_label("Category name").fill("Fridge")
    category(page, "Dairy").get_by_role("button", name="Rename").click()
    expect(category(page, "Fridge")).to_be_visible()

    category(page, "Fridge").get_by_label("Category actions").click()
    category(page, "Fridge").get_by_role("button", name="Clear done").click()
    expect(items_of(page, "Fridge")).to_have_text([re.compile("Yogurt")])

    category(page, "Fridge").get_by_label("Category actions").click()
    confirms: list[str] = []
    page.once("dialog", lambda d: (confirms.append(d.message), d.accept()))
    category(page, "Fridge").get_by_role("button", name="Archive category").click()
    expect(category(page, "Fridge")).to_have_count(0)
    expect(page.locator("#toasts")).to_contain_text("Category archived.")
    assert confirms == [
        "Archive “Fridge” with its items? You can restore it from the board's archive."
    ]

    page.reload()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery"])


def test_archive_page_restores_and_deletes_permanently(  # Review Focus 3
    logged_in_page, seed, user, open_board
):
    page = logged_in_page

    async def make(db) -> int:
        board = await create_board(db, user, "Groceries")
        await create_item(db, await create_category(db, board, "Bakery"), "Bread")
        yesterday = datetime.now(UTC) - timedelta(days=1)
        dairy = await create_category(db, board, "Dairy", archived_at=yesterday)
        await create_item(db, dairy, "Milk")
        await create_category(db, board, "Łosoś", archived_at=yesterday)
        return board.id

    board_id = seed(make)
    problems = watch_console(page)
    open_board(page, board_id)
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery"])

    page.get_by_role("link", name="Archived categories").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Archived categories in Groceries")
    dairy = page.locator("li").filter(has=page.get_by_role("heading", name="Dairy", exact=True))
    expect(dairy).to_contain_text("1 item ·")
    dairy.get_by_role("button", name="Restore").click()
    expect(page.locator("#toasts")).to_contain_text("Category restored.")
    expect(page.get_by_role("heading", name="Dairy", exact=True)).to_have_count(0)

    prompts: list[str] = []
    page.once("dialog", lambda d: (prompts.append(d.message), d.accept("Łosoś")))
    page.get_by_role("button", name="Delete permanently").click()
    expect(page.get_by_text("No archived categories.")).to_be_visible()
    expect(page.locator("#toasts")).to_contain_text("Category deleted permanently.")
    assert prompts == ["Type “Łosoś” to delete this category and its items permanently."]

    page.get_by_role("link", name="Back to the board").click()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery", "Dairy"])
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk")])
    assert problems == []


def test_dragging_the_handle_reorders_categories(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)

    with page.expect_response(re.compile(r"/categories/order$")) as saved:
        category(page, "Bakery").locator(".category-handle").drag_to(
            category(page, "Dairy").locator(".category-handle")
        )

    assert saved.value.status == 204
    page.reload()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery", "Dairy"])


def test_ticking_an_item_deleted_on_another_device_resyncs_the_board(
    logged_in_page, seed, user, open_board
):  # Review Focus 2
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk", "Yogurt"]})
    open_board(page, board_id)

    async def other_device(db) -> None:
        # Deleted on the other device, as a delete there leaves it: a marked row.
        await db.execute(update(Item).where(Item.name == "Milk").values(deleted_at=func.now()))
        bakery = Category(board_id=board_id, name="Bakery", position=1)
        db.add(bakery)
        await db.commit()

    seed(other_device)
    page.get_by_role("checkbox", name="Milk").click()

    expect(page.locator("#toasts")).to_have_text("This item no longer exists.")
    expect(page.get_by_role("checkbox", name="Milk")).to_have_count(0)
    expect(category(page, "Bakery")).to_be_visible()  # the whole board was reloaded
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Yogurt")])


@pytest.mark.parametrize(
    ("width", "columns"),
    [(375, "auto"), (768, "2"), (1024, "2"), (1279, "2"), (1280, "3")],  # 3 from 1280 px (xl)
)
def test_categories_flow_into_columns(signed_in_page, seed, user, width, columns, open_board):
    board_id = seed_board(seed, user, {name: [] for name in ("Dairy", "Bakery", "Fruit")})
    page = signed_in_page(viewport={"width": width, "height": 900})

    open_board(page, board_id)

    column_count = page.locator("#categories").evaluate("(el) => getComputedStyle(el).columnCount")
    assert column_count == columns


# One range per word of a category title: its text node's offsets, then how many line boxes the word
# has. A word that fits on a line has one; a word the browser broke in the middle has two.
LINES_OF_EACH_WORD = """(heading) => {
  const walker = document.createTreeWalker(heading, NodeFilter.SHOW_TEXT);
  const lines = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (const word of node.data.matchAll(/\\S+/g)) {
      const range = document.createRange();
      range.setStart(node, word.index);
      range.setEnd(node, word.index + word[0].length);
      lines.push([word[0], range.getClientRects().length]);
    }
  }
  return lines;
}"""


@pytest.mark.parametrize("width", [375, 768, 1024, 1280, 1440])
def test_long_category_title_does_not_break_a_word(signed_in_page, seed, user, width, open_board):
    title = "Artykuły gospodarstwa domowego"
    board_id = seed_board(seed, user, {title: ["Płyn do naczyń"], "Dairy": []})
    page = signed_in_page(viewport={"width": width, "height": 900})

    open_board(page, board_id)

    section = category(page, title)
    buttons = section.locator("div.flex > .btn-icon, div.flex > details > summary.btn-icon")
    assert buttons.count() == 4  # the handle, hide, sort and ⋮: an editor's four buttons
    for button in buttons.all():
        box = button.bounding_box()
        assert box is not None
        assert box["width"] >= 44
        assert box["height"] >= 44
    broken = [
        word
        for word, lines in section.get_by_role("heading", name=title).evaluate(LINES_OF_EACH_WORD)
        if lines != 1
    ]
    assert broken == [], f"at {width} px these words are split over lines: {broken}"


@pytest.mark.parametrize("width", [768, 1280, 1440])
@pytest.mark.parametrize("page_name", ["board", "board list"])
def test_header_and_content_have_the_same_width(
    signed_in_page, seed, user, width, page_name, open_board
):
    """The board page is wider than the others (room for three columns); its header goes with it."""
    board_id = seed_board(seed, user, {"Dairy": []})
    page = signed_in_page(viewport={"width": width, "height": 900})

    if page_name == "board":
        open_board(page, board_id)
    else:
        page.goto("/boards")

    header = page.locator("header > div").bounding_box()
    content = page.locator("main").bounding_box()
    assert header is not None
    assert content is not None
    assert (header["x"], header["width"]) == (content["x"], content["width"])


TOGGLED = re.compile(r"/items/\d+/toggle$")
MOVED = re.compile(r"/items/\d+/move$")
ORDER_SAVED = re.compile(r"/categories/order$")


def category_names(page: Page) -> Locator:
    return page.locator("section[data-category-id] h2")


def test_checkbox_keeps_keyboard_focus_across_a_tick(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk", "Yogurt"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    milk.focus()

    with page.expect_response(TOGGLED):
        page.keyboard.press("Space")

    expect(milk).to_be_checked()
    expect(milk).to_be_focused()  # the category was swapped: the same checkbox has it again
    with page.expect_response(TOGGLED):
        page.keyboard.press("Space")
    expect(milk).not_to_be_checked()


def test_tick_sends_the_state_its_user_wants(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")

    with page.expect_request(TOGGLED) as ticked:
        milk.click()
    expect(milk).to_be_checked()
    with page.expect_request(TOGGLED) as unticked:
        milk.click()
    expect(milk).not_to_be_checked()

    assert parse_qs(ticked.value.post_data)["done"] == ["true"]
    assert parse_qs(unticked.value.post_data)["done"] == ["false"]


def test_untick_while_the_tick_is_on_its_way_is_sent(logged_in_page, seed, user, open_board):
    """A quick double tap: the second one lands before the first one's answer has replaced the
    category. It still reaches the server (once), and the board ends up as its user left it."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    sent: list[Request] = []
    held: list[Route] = []

    def answer(route: Route) -> None:
        sent.append(route.request)
        # The first answer waits for the second tap; anything after it goes through.
        route.continue_() if held else held.append(route)

    page.route(TOGGLED, answer)

    with page.expect_request(TOGGLED) as ticked:
        milk.click()
    while not held:
        page.wait_for_timeout(10)
    milk.click()
    with page.expect_request(TOGGLED) as unticked:
        held[0].continue_()

    assert parse_qs(ticked.value.post_data)["done"] == ["true"]
    assert parse_qs(unticked.value.post_data)["done"] == ["false"]
    unticked.value.response()
    expect(milk).not_to_be_checked()
    page.reload()
    expect(milk).not_to_be_checked()
    assert len(sent) == 2  # the one htmx held for the replaced checkbox was not sent as well


@pytest.mark.parametrize("failure", ["error", "no answer"])
def test_untick_held_behind_a_failed_tick_is_sent(logged_in_page, seed, user, open_board, failure):
    """A quick double tap whose first request fails: the second tap is still sent, with the state
    its user left (unticked); the failed tick must not turn the checkbox back on first."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    sent: list[Request] = []
    held: list[Route] = []

    def answer(route: Route) -> None:
        sent.append(route.request)
        route.continue_() if held else held.append(route)  # the first waits, the rest go on

    page.route(TOGGLED, answer)

    milk.click()
    while not held:
        page.wait_for_timeout(10)
    milk.click()  # waits in htmx's queue for the first answer
    with page.expect_request(TOGGLED) as second:
        if failure == "error":
            held[0].fulfill(status=500, body="boom")
        else:
            held[0].abort()

    assert parse_qs(second.value.post_data)["done"] == ["false"]
    second.value.response()
    expect(milk).not_to_be_checked()
    page.reload()
    expect(milk).not_to_be_checked()
    assert len(sent) == 2


@pytest.mark.parametrize("failure", ["error", "no answer"])
@pytest.mark.parametrize("start_done", [False, True], ids=["unticked", "ticked"])
def test_two_failed_taps_leave_the_state_the_server_has(
    logged_in_page, seed, user, open_board, failure, start_done
):
    """A quick double tap whose two requests both fail: the first leaves the checkbox as tapped
    (the second is sent next), the second puts it back. The user's last tap equals the state they
    started from, and it is the saved one that must stay on screen, struck or not."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]}, done=("Milk",) if start_done else ())
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    sent: list[Request] = []
    held: list[Route] = []

    def fail(route: Route) -> None:
        if failure == "error":
            route.fulfill(status=500, body="boom")
        else:
            route.abort()

    def answer(route: Route) -> None:
        sent.append(route.request)
        fail(route) if held else held.append(route)  # the first waits for the second tap

    page.route(TOGGLED, answer)

    milk.click()
    while not held:
        page.wait_for_timeout(10)
    milk.click()  # waits in htmx's queue for the first answer
    fail(held[0])

    expect(page.locator("#toasts > *")).to_have_count(2)  # both failures are in
    expect(milk).to_be_checked(checked=start_done)
    expect_struck(item(page, "Milk"), start_done)
    page.reload()
    expect(milk).to_be_checked(checked=start_done)
    expect_struck(item(page, "Milk"), start_done)
    assert len(sent) == 2


def test_untick_held_while_another_answer_replaced_the_category_is_sent(
    logged_in_page, seed, user, open_board
):
    """The category is replaced by the answer to something else (adding an item) before the tick
    was saved: the new "Milk" is unticked, as the server still has it, yet its user unticked it."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    held: list[Route] = []
    page.route(TOGGLED, lambda route: held.append(route))  # every tick waits here, unanswered

    milk.click()
    while not held:
        page.wait_for_timeout(10)
    milk.click()
    dairy = category(page, "Dairy")
    dairy.get_by_placeholder("Add item").fill("Eggs")
    with page.expect_request(TOGGLED, timeout=2000) as second:
        dairy.get_by_role("button", name="Add", exact=True).click()

    assert parse_qs(second.value.post_data)["done"] == ["false"]
    for _ in range(100):  # the route may see the request a moment after the page reports it
        if len(held) == 2:
            break
        page.wait_for_timeout(10)
    for route in held:  # the server gets them in turn
        with page.expect_response(TOGGLED):
            route.continue_()
    page.reload()
    expect(milk).not_to_be_checked()
    assert len(held) == 2  # what htmx queued for the replaced checkbox it dropped


@pytest.mark.parametrize("failure", ["error", "no answer"])
def test_drag_that_did_not_go_through_puts_the_item_back(
    logged_in_page, seed, user, open_board, failure
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk", "Yogurt"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    page.route(
        MOVED,
        lambda route: (
            route.fulfill(status=500, body="boom") if failure == "error" else route.abort()
        ),
    )

    bread = items_of(page, "Bakery").first
    with page.expect_request(MOVED):
        below_centre = {"x": 20, "y": bread.bounding_box()["height"] - 6}
        item(page, "Milk").drag_to(bread, target_position=below_centre)

    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread")])  # the board reloaded
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk"), re.compile("Yogurt")])


def test_reorder_that_did_not_go_through_puts_the_categories_back(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    page.route(ORDER_SAVED, lambda route: route.fulfill(status=500, body="boom"))

    with page.expect_request(ORDER_SAVED):
        category(page, "Bakery").locator(".category-handle").drag_to(
            category(page, "Dairy").locator(".category-handle")
        )

    expect(category_names(page)).to_have_text(["Dairy", "Bakery"])


def test_categories_move_up_and_down_from_the_keyboard(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": [], "Bakery": [], "Fruit": []})
    open_board(page, board_id)
    bakery = category(page, "Bakery")
    bakery.get_by_label("Category actions").press("Enter")  # opens the menu

    with page.expect_response(ORDER_SAVED) as saved:
        bakery.get_by_role("button", name="Move up").press("Enter")
    assert saved.value.status == 204
    expect(category_names(page)).to_have_text(["Bakery", "Dairy", "Fruit"])
    expect(bakery.get_by_role("button", name="Move up")).to_be_hidden()  # first: nowhere to go
    expect(bakery.get_by_role("button", name="Move down")).to_be_focused()

    with page.expect_response(ORDER_SAVED):
        page.keyboard.press("Enter")
    with page.expect_response(ORDER_SAVED):
        page.keyboard.press("Enter")
    expect(category_names(page)).to_have_text(["Dairy", "Fruit", "Bakery"])
    expect(bakery.get_by_role("button", name="Move down")).to_be_hidden()  # last

    page.reload()
    expect(category_names(page)).to_have_text(["Dairy", "Fruit", "Bakery"])


def test_category_handle_drags_on_a_touch_screen(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)

    with page.expect_response(ORDER_SAVED) as saved:
        touch_drag(
            page,
            category(page, "Bakery").locator(".category-handle"),
            category(page, "Dairy").locator(".category-handle"),
            # The handle does not scroll the page: a finger on it drags at once. Bakery has landed
            # when it is the section just before Dairy.
            landed=category(page, "Dairy")
            .locator("xpath=preceding-sibling::section")
            .and_(page.locator(PICKED_UP)),
        )

    assert saved.value.status == 204
    expect(category_names(page)).to_have_text(["Bakery", "Dairy"])


def test_tap_beside_the_item_modal_asks_before_dropping_edits(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    page.get_by_role("button", name="Milk", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(page.locator("#board-content")).to_have_attribute("inert", "")  # the page behind
    beside = {"x": 5, "y": 5}  # on the dimmed page around the modal

    page.mouse.click(**beside)  # nothing changed: it just closes
    expect(dialog).to_be_hidden()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")

    page.get_by_role("button", name="Milk", exact=True).click()
    dialog.get_by_label("Name").fill("Oat milk")
    questions: list[str] = []

    def answer(question, accept: bool) -> None:
        questions.append(question.message)
        question.accept() if accept else question.dismiss()

    page.once("dialog", lambda question: answer(question, accept=False))
    page.mouse.click(**beside)
    expect(dialog.get_by_label("Name")).to_have_value("Oat milk")  # kept
    page.once("dialog", lambda question: answer(question, accept=True))
    page.mouse.click(**beside)
    expect(dialog).to_be_hidden()

    assert questions == ["Discard your changes to this item?"] * 2
    expect(item(page, "Milk")).to_be_visible()  # nothing was saved


def test_item_modal_and_the_inert_page_behind_it_change_together(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    page.clock.install()  # time runs as usual until it is paused
    open_board(page, board_id)
    dialog = page.get_by_role("dialog")
    # From here on no animation frame (nor timer) runs in the page, as on a busy phone (or CI
    # runner) whose next frame is late: the modal must show and hide without waiting for one, as
    # the page behind turns inert and back at once (a tap in between landed on that inert page,
    # and the modal opened after it).
    page.clock.pause_at(datetime.now() + timedelta(seconds=1))

    page.get_by_role("button", name="Milk", exact=True).click()
    expect(dialog).to_be_visible()
    expect(page.locator("#board-content")).to_have_attribute("inert", "")

    page.mouse.click(x=5, y=5)  # beside it, at once
    expect(dialog).to_be_hidden()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")


def test_menus_close_on_esc_and_outside_clicks_one_open_at_a_time(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    open_menus = page.locator("details[open]")
    dairy_menu = category(page, "Dairy").get_by_label("Category actions")

    dairy_menu.click()
    expect(open_menus).to_have_count(1)
    category(page, "Bakery").get_by_label("Category actions").click()
    expect(open_menus).to_have_count(1)  # the other one closed
    bakery_actions = category(page, "Bakery").locator(
        "details", has=page.get_by_label("Category actions")
    )
    expect(bakery_actions).to_have_attribute("open", "")

    page.keyboard.press("Escape")
    expect(open_menus).to_have_count(0)
    expect(category(page, "Bakery").get_by_label("Category actions")).to_be_focused()

    dairy_menu.click()
    expect(open_menus).to_have_count(1)
    page.get_by_role("heading", level=1).click()  # anywhere outside it
    expect(open_menus).to_have_count(0)

    # The sort menu is one of them: opening it closes the actions menu, and the other way round.
    category(page, "Dairy").get_by_label("Sort Dairy").click()
    expect(open_menus).to_have_count(1)
    dairy_menu.click()
    expect(open_menus).to_have_count(1)
    expect(category(page, "Dairy").locator("details[open]")).to_have_count(1)
    expect(category(page, "Dairy").get_by_role("button", name="Rename")).to_be_visible()
    expect(category(page, "Dairy").get_by_role("button", name="Apply")).to_be_hidden()


def test_folded_category_stays_folded_on_this_device(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    fold = category(page, "Dairy").get_by_role("button", name="Hide items")

    fold.click()

    expect(page.get_by_role("checkbox", name="Milk")).to_be_hidden()
    expect(page.get_by_role("checkbox", name="Bread")).to_be_visible()
    unfold = category(page, "Dairy").get_by_role("button", name="Show items")
    expect(unfold).to_have_attribute("aria-expanded", "false")
    page.reload()
    expect(page.get_by_role("checkbox", name="Milk")).to_be_hidden()
    unfold.click()
    expect(page.get_by_role("checkbox", name="Milk")).to_be_visible()
    page.reload()
    expect(page.get_by_role("checkbox", name="Milk")).to_be_visible()


def test_sort_menu_reorders_one_category_and_remembers_it(logged_in_page, seed, user, open_board):
    page = logged_in_page
    problems = watch_console(page)
    board_id = seed_board(
        seed, user, {"Dairy": ["Butter", "Milk", "Yogurt"], "Bakery": ["Bagel", "Bread"]}
    )
    open_board(page, board_id)
    dairy_sort = category(page, "Dairy").get_by_label("Sort Dairy")
    expect(dairy_sort).not_to_have_class(re.compile("sort-custom"))
    expect(dairy_sort).to_have_attribute("title", "Sort")

    sort_dairy(page, "Z–A / newest first")

    expect(items_of(page, "Dairy")).to_have_text(
        [re.compile("Yogurt"), re.compile("Milk"), re.compile("Butter")]
    )
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bagel"), re.compile("Bread")])
    expect(category(page, "Bakery").get_by_label("Sort Bakery")).not_to_have_class(
        re.compile("sort-custom")
    )
    expect(dairy_sort).to_have_attribute("title", "Custom order")
    expect(page.locator("details[open]")).to_have_count(0)  # the menu closed with the swap

    page.reload()  # kept for this account, not only for this page
    expect(items_of(page, "Dairy")).to_have_text(
        [re.compile("Yogurt"), re.compile("Milk"), re.compile("Butter")]
    )
    expect(dairy_sort).to_have_class(re.compile("sort-custom"))
    dairy_sort.click()  # the menu shows the choice
    expect(category(page, "Dairy").get_by_role("radio", name="Z–A / newest first")).to_be_checked()
    expect(category(page, "Dairy").get_by_role("radio", name="Name", exact=True)).to_be_checked()

    category(page, "Dairy").get_by_role("button", name="Use my default").click()

    expect(items_of(page, "Dairy")).to_have_text(
        [re.compile("Butter"), re.compile("Milk"), re.compile("Yogurt")]
    )
    expect(dairy_sort).not_to_have_class(re.compile("sort-custom"))
    page.reload()
    expect(items_of(page, "Dairy")).to_have_text(
        [re.compile("Butter"), re.compile("Milk"), re.compile("Yogurt")]
    )
    assert problems == []


def test_move_category_to_a_board_with_the_same_name(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    problems = watch_console(page)
    groceries = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, groceries)
    category(page, "Dairy").get_by_label("Category actions").click()
    # Nowhere to move it to: the board is the user's only one.
    expect(category(page, "Dairy").locator("[data-move-category-id]")).to_be_hidden()
    expect(page.locator("#move-category")).to_have_count(0)

    async def more_boards(db) -> int:
        vic = await create_user(db, "vic@example.com")
        weekend = await create_board(db, user, "Weekend")
        await create_item(db, await create_category(db, weekend, "Dairy"), "Cream")
        await create_board(db, vic, "Vic's", {user: BoardRole.VIEWER})  # one to read, not to edit
        return weekend.id

    weekend = seed(more_boards)
    open_board(page, groceries)
    category(page, "Dairy").get_by_label("Category actions").click()
    category(page, "Dairy").get_by_role("button", name="Move to board…").click()
    dialog = page.get_by_role("dialog", name="Move to board")
    expect(dialog).to_be_visible()
    expect(dialog.locator("select option")).to_have_text(["Weekend"])
    expect(dialog.get_by_label("Category name")).to_have_value("Dairy")
    expect(dialog.get_by_label("Board")).to_be_focused()

    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(category(page, "Dairy").get_by_label("Category actions")).to_be_focused()

    category(page, "Dairy").get_by_label("Category actions").click()
    category(page, "Dairy").get_by_role("button", name="Move to board…").click()
    dialog.get_by_role("button", name="Move", exact=True).click()

    # Weekend has a "Dairy": the dialog stays, with the free name in the field and the reason.
    expect(dialog.get_by_label("Category name")).to_have_value("Dairy (2)")
    expect(dialog.get_by_label("Category name")).to_be_focused()
    expect(dialog.get_by_role("alert")).to_have_text(
        "That board already has a category with this name."
    )
    assert test_responsive.too_small_targets(page) == []
    assert page.evaluate(test_responsive.LOW_CONTRAST_TEXT, test_responsive.AA) == []

    with page.expect_response(re.compile(r"/categories/\d+/move$")) as moved:
        dialog.get_by_role("button", name="Move", exact=True).click()

    assert moved.value.status == 200
    expect(dialog).to_be_hidden()
    expect(category(page, "Dairy")).to_have_count(0)
    expect(page.locator("#toasts")).to_contain_text(
        "Moved to “Weekend”."
    )  # the 409's may still show
    page.goto(f"/boards/{weekend}")
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Dairy", "Dairy (2)"])
    expect(items_of(page, "Dairy (2)")).to_have_text([re.compile("Milk")])
    assert all("409" in problem for problem in problems), problems  # the one refused move


def test_taken_name_alert_goes_when_another_board_is_picked(logged_in_page, seed, user, open_board):
    page = logged_in_page

    async def make(db) -> int:
        groceries = await create_board(db, user, "Groceries")
        await create_item(db, await create_category(db, groceries, "Dairy"), "Milk")
        weekend = await create_board(db, user, "Weekend")
        await create_category(db, weekend, "Dairy")
        await create_board(db, user, "Hardware")  # no "Dairy" there
        return groceries.id

    open_board(page, seed(make))
    category(page, "Dairy").get_by_label("Category actions").click()
    category(page, "Dairy").get_by_role("button", name="Move to board…").click()
    dialog = page.get_by_role("dialog", name="Move to board")
    expect(dialog.locator("select option")).to_have_text(["Hardware", "Weekend"])
    dialog.get_by_label("Board").select_option(label="Weekend")
    taken = dialog.locator("#move-category-taken")
    expect(taken).to_be_hidden()

    dialog.get_by_role("button", name="Move", exact=True).click()

    expect(dialog.get_by_label("Category name")).to_have_value("Dairy (2)")
    expect(taken).to_be_visible()  # said about Weekend

    dialog.get_by_label("Board").select_option(label="Hardware")

    expect(taken).to_be_hidden()  # not about Hardware
    expect(dialog).to_be_visible()
