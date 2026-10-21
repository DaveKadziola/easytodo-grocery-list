"""The board page in Chromium: the phone layout, adding and ticking items, drag and drop (mouse
and touch), the item modal, the category menu, renaming and deleting the board, a change that
lost a race with another device, and no CSP violations anywhere."""

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import pytest
from factories import create_board, create_category, create_item
from playwright.sync_api import Locator, Page, expect
from sqlalchemy import delete, update

from e2e.conftest import watch_console
from easytodo.models import Category, Item

pytestmark = pytest.mark.e2e

NASTY = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
# What a finger must be able to hit: every control at least 44×44 px (the checkbox through the
# label around it).
TAP_TARGETS = (
    "a, button, summary, select, input:not([type=checkbox]), label:has(> input[type=checkbox])"
)
# On a phone the bottom bar covers the foot of the screen: what a finger goes for is scrolled up.
SCROLL_CLEAR_OF_THE_BOTTOM_BAR = "(el) => el.scrollIntoView({ block: 'center' })"


def seed_board(seed, owner, categories: dict[str, list[str]], done: tuple[str, ...] = ()) -> int:
    """A board "Groceries" of ``owner`` with these categories (in this order) and items, the ones
    named in ``done`` ticked; returns the board's id."""

    async def make(db) -> int:
        board = await create_board(db, owner, "Groceries")
        for position, (name, items) in enumerate(categories.items()):
            category = await create_category(db, board, name, position)
            for item_name in items:
                await create_item(db, category, item_name, is_done=item_name in done)
        return board.id

    return seed(make)


def describe(seed, item_name: str, description: str) -> None:
    async def run(db) -> None:
        await db.execute(update(Item).where(Item.name == item_name).values(description=description))
        await db.commit()

    seed(run)


def category(page: Page, name: str) -> Locator:
    return page.locator("section[data-category-id]").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )


def item(page: Page, name: str) -> Locator:
    return page.locator("li[data-item-id]").filter(
        has=page.get_by_role("button", name=name, exact=True)
    )


def items_of(page: Page, category_name: str) -> Locator:
    return category(page, category_name).locator("li[data-item-id]")


def too_small_targets(page: Page) -> list[str]:
    return page.locator("#board-content").evaluate(
        """(board, selector) => Array.from(board.querySelectorAll(selector))
            .filter((el) => el.getClientRects().length > 0)
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


def touch_drag(page: Page, source: Locator, target: Locator, hold_ms: int) -> None:
    """A finger on ``source``, held for ``hold_ms``, then moved onto ``target`` and lifted."""
    target.evaluate(SCROLL_CLEAR_OF_THE_BOTTOM_BAR)
    start, end = source.bounding_box(), target.bounding_box()
    touch = page.context.new_cdp_session(page)

    def finger(kind: str, x: float | None = None, y: float | None = None) -> None:
        points = [] if x is None else [{"x": x, "y": y}]
        touch.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    x = start["x"] + start["width"] / 2
    finger("touchStart", x, start["y"] + 10)
    page.wait_for_timeout(hold_ms)
    for step in range(1, 11):
        finger("touchMove", x, start["y"] + 10 + (end["y"] - start["y"]) * step / 10)
        page.wait_for_timeout(20)
    # Rest on the target before lifting: Sortable looks under the finger every 50 ms.
    page.wait_for_timeout(200)
    finger("touchEnd")


def test_long_press_drags_an_item_on_a_touch_screen(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    board_id = seed_board(seed, user, {"Dairy": ["Milk"], "Bakery": ["Bread"]})
    open_board(page, board_id)
    moves: list[str] = []
    page.on(
        "request", lambda request: moves.append(request.url) if "/move" in request.url else None
    )

    touch_drag(page, item(page, "Milk"), item(page, "Bread"), hold_ms=0)  # a swipe scrolls

    page.wait_for_timeout(300)
    assert moves == []
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Milk")])

    with page.expect_response(re.compile(r"/items/\d+/move$")) as moved:
        touch_drag(page, item(page, "Milk"), item(page, "Bread"), hold_ms=400)  # a long press

    assert moved.value.status == 200
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])


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
    open_board(page, board_id)  # as the owner: rename and delete are in the board's menu
    page.get_by_label("Board actions").click()
    expect(page.get_by_role("button", name="Delete board")).to_be_visible()
    page.keyboard.press("Escape")
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
    category(page, "Dairy").locator("summary").click()
    expect(category(page, "Dairy").get_by_role("button", name="Clear done (1)")).to_be_visible()

    assert [p for p in problems if "Content Security Policy" in p or "Refused to" in p] == []
    assert problems == [] and board_problems == []


def test_owner_renames_and_deletes_the_board(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    problems = watch_console(page)
    statuses: list[int] = []
    page.on("response", lambda response: statuses.append(response.status))

    expect(page.get_by_label("Board name")).to_be_hidden()  # in the menu, not on the page
    page.get_by_label("Board actions").click()
    page.get_by_label("Board name").fill("Weekend")
    page.locator("form", has=page.get_by_label("Board name")).get_by_role(
        "button", name="Rename"
    ).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Weekend")
    expect(page.locator("#board-content")).to_have_attribute("data-board-name", "Weekend")

    page.get_by_label("Board actions").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete board").click()

    expect(page).to_have_url(re.compile(r"/boards$"))
    expect(page.get_by_text("You have no boards yet.")).to_be_visible()
    assert 403 not in statuses
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


def test_new_category_goes_last_and_the_field_is_ready_for_the_next(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {})
    open_board(page, board_id)
    expect(page.get_by_text("Add your first category")).to_be_visible()
    field = page.get_by_placeholder("New category")

    for name in ("Dairy", "Bakery"):
        field.fill(name)
        field.press("Enter")
        expect(category(page, name)).to_be_visible()
        expect(field).to_have_value("")
        expect(field).to_be_focused()

    expect(page.get_by_text("Add your first category")).to_be_hidden()
    page.reload()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Dairy", "Bakery"])


def test_category_menu_renames_clears_done_and_deletes(logged_in_page, seed, user, open_board):
    page = logged_in_page
    board_id = seed_board(
        seed, user, {"Dairy": ["Milk", "Yogurt"], "Bakery": ["Bread"]}, done=("Milk",)
    )
    open_board(page, board_id)

    category(page, "Dairy").locator("summary").click()
    category(page, "Dairy").get_by_label("Category name").fill("Fridge")
    category(page, "Dairy").get_by_role("button", name="Rename").click()
    expect(category(page, "Fridge")).to_be_visible()

    category(page, "Fridge").locator("summary").click()
    category(page, "Fridge").get_by_role("button", name="Clear done").click()
    expect(items_of(page, "Fridge")).to_have_text([re.compile("Yogurt")])

    category(page, "Fridge").locator("summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    category(page, "Fridge").get_by_role("button", name="Delete category").click()
    expect(category(page, "Fridge")).to_have_count(0)

    page.reload()
    expect(page.locator("section[data-category-id] h2")).to_have_text(["Bakery"])


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
        await db.execute(delete(Item).where(Item.name == "Milk"))
        bakery = Category(board_id=board_id, name="Bakery", position=1)
        db.add(bakery)
        await db.commit()

    seed(other_device)
    page.get_by_role("checkbox", name="Milk").click()

    expect(page.locator("#toasts")).to_have_text("This item no longer exists.")
    expect(page.get_by_role("checkbox", name="Milk")).to_have_count(0)
    expect(category(page, "Bakery")).to_be_visible()  # the whole board was reloaded
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Yogurt")])


@pytest.mark.parametrize(("width", "columns"), [(375, "auto"), (768, "2"), (1280, "3")])
def test_categories_flow_into_columns(signed_in_page, seed, user, width, columns, open_board):
    board_id = seed_board(seed, user, {name: [] for name in ("Dairy", "Bakery", "Fruit")})
    page = signed_in_page(viewport={"width": width, "height": 900})

    open_board(page, board_id)

    column_count = page.locator("#categories").evaluate("(el) => getComputedStyle(el).columnCount")
    assert column_count == columns


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
    category. It still reaches the server, and the board ends up as its user left it."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    milk = page.get_by_role("checkbox", name="Milk")
    held = []
    # The first answer waits for the second tap; anything after it goes through.
    page.route(TOGGLED, lambda route: route.continue_() if held else held.append(route))

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
            hold_ms=0,  # the handle does not scroll the page: a finger on it drags at once
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
    expect(category(page, "Bakery").locator("details")).to_have_attribute("open", "")

    page.keyboard.press("Escape")
    expect(open_menus).to_have_count(0)
    expect(category(page, "Bakery").get_by_label("Category actions")).to_be_focused()

    dairy_menu.click()
    expect(open_menus).to_have_count(1)
    page.get_by_role("heading", level=1).click()  # anywhere outside it
    expect(open_menus).to_have_count(0)


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
