"""Live board updates in Chromium: changes on one device show on the others without reloading,
without getting in the way of what the user is doing there (typing, the item modal, a click or
a drag in progress, a request of its own), and the page follows the server when it signs the
device out or the member is removed; after a dropped connection it reconnects and catches up."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from factories import create_board, create_category, create_item, create_user
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect
from sqlalchemy import delete, select, update

from e2e.conftest import open_board_menu
from e2e.locators import add_item, category, item, items_of
from easytodo.content import service
from easytodo.models import BoardMember, BoardRole, Category, Item
from easytodo.realtime.events import commit_and_publish

pytestmark = pytest.mark.e2e

LIVE_UPDATE_MS = 2000  # how soon a change must show on the other device
SETTLE_MS = 1000  # long enough for an update that is (wrongly) not held back to show
MOVED = re.compile(r"/items/\d+/move$")
TOGGLED = re.compile(r"/items/\d+/toggle$")
DETAILS = re.compile(r"/items/\d+/details$")


def received_messages(page: Page) -> list[dict]:
    """Every message the page's WebSockets receive from now on (set up before it connects)."""
    messages: list[dict] = []
    page.on(
        "websocket",
        lambda ws: ws.on("framereceived", lambda payload: messages.append(json.loads(payload))),
    )
    return messages


def centre(locator: Locator) -> tuple[float, float]:
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def seed_two_categories(seed, user) -> int:
    """A board "Groceries" of ``user``: "Dairy" with "Milk", then "Bakery" with "Bread"."""

    async def make(db):
        board = await create_board(db, user, "Groceries")
        await create_item(db, await create_category(db, board, "Dairy", 0), "Milk")
        await create_item(db, await create_category(db, board, "Bakery", 1), "Bread")
        return board.id

    return seed(make)


def routed_sockets(page: Page) -> list[WebSocketRoute]:
    """The board sockets that ``page`` opens from now on, each passed through to the server;
    ``close()`` on one drops that connection (on the page's side)."""
    sockets: list[WebSocketRoute] = []

    def connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.connect_to_server()

    page.route_web_socket(re.compile(r"/ws/boards/\d+"), connect)
    return sockets


def set_visibility(page: Page, state: str) -> None:
    """Make ``page`` look hidden or visible again (the tab in the background, the phone locked),
    as the browser does: document.visibilityState, then a visibilitychange event."""
    page.evaluate(
        """(state) => {
            Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
            Object.defineProperty(document, "hidden", { configurable: true, get: () => state === "hidden" });
            document.dispatchEvent(new Event("visibilitychange"));
        }""",
        state,
    )


def board_reloads(page: Page) -> str | None:
    """How many times the board content was reloaded so far (see COUNT_BOARD_RELOADS)."""
    return page.locator("html").get_attribute("data-board-reloads")


def wait_until(page: Page, condition: Callable[[], object], what: str) -> None:
    """Wait (up to 2 s) until ``condition()`` holds, letting ``page``'s events come in."""
    for _ in range(40):
        if condition():
            return
        page.wait_for_timeout(50)
    pytest.fail(f"{what} did not happen")


def wait_for_message(page: Page, messages: list[dict], type_: str) -> None:
    wait_until(
        page,
        lambda: any(message["type"] == type_ for message in messages),
        f"a {type_} message ({messages})",
    )


def test_second_browser_sees_new_item_within_2s(two_logged_in_pages):
    a, b = two_logged_in_pages

    add_item(a, "Dairy", "Bread")

    expect(b.get_by_role("checkbox", name="Bread")).to_be_visible(timeout=LIVE_UPDATE_MS)
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_rename_shows_on_the_other_device_without_reload(two_logged_in_pages):
    a, b = two_logged_in_pages
    b.evaluate("window.notReloaded = true")

    a.goto("/boards")  # renaming is in the board's ⋮ menu on the list
    card = open_board_menu(a, "Groceries")
    card.get_by_label("Board name").fill("Weekend")
    card.get_by_role("button", name="Rename").click()

    expect(b.get_by_role("heading", level=1)).to_have_text("Weekend", timeout=LIVE_UPDATE_MS)
    expect(b).to_have_title("Weekend · EasyToDo")
    assert b.evaluate("window.notReloaded") is True


def test_typing_in_a_field_is_not_interrupted_by_a_remote_change(two_logged_in_pages):
    a, b = two_logged_in_pages
    field = category(b, "Dairy").get_by_placeholder("Add item")
    field.fill("Chee")

    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)

    expect(field).to_have_value("Chee")
    expect(field).to_be_focused()
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")])  # held back while typing
    b.get_by_role("heading", level=1).click()  # done typing
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_update_answered_once_typing_started_waits_for_the_typing_to_end(two_logged_in_pages):
    a, b = two_logged_in_pages
    held: list[Route] = []
    # The update's request is sent while nobody types, but answered only once someone does.
    b.route(
        re.compile(r"/categories/\d+$"),
        lambda route: route.continue_() if held else held.append(route),
    )
    add_item(a, "Dairy", "Bread")
    wait_until(b, lambda: held, "the update's request")
    field = category(b, "Dairy").get_by_placeholder("Add item")
    field.fill("Chee")

    held[0].continue_()
    b.wait_for_timeout(SETTLE_MS)

    expect(field).to_have_value("Chee")
    expect(field).to_be_focused()
    b.get_by_role("heading", level=1).click()  # done typing: fetched again
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_remote_change_waits_for_the_item_modal_to_close(two_logged_in_pages):
    a, b = two_logged_in_pages
    b.get_by_role("button", name="Milk", exact=True).click()
    dialog = b.get_by_role("dialog")
    dialog.get_by_label("Name").fill("Oat milk")

    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)

    expect(dialog.get_by_label("Name")).to_have_value("Oat milk")
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")])  # held back while editing
    dialog.get_by_role("button", name="Cancel").click()
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_remote_change_waits_for_the_new_category_dialog_to_close(two_logged_in_pages):
    a, b = two_logged_in_pages
    b.get_by_role("button", name="New category").click()
    dialog = b.get_by_role("dialog", name="New category")
    expect(dialog).to_be_visible()

    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)

    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")])  # held back, as with the modal
    expect(b.locator("#board-content")).to_have_attribute("inert", "")  # and still inert
    dialog.get_by_role("button", name="Cancel").click()
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    expect(b.locator("#board-content")).not_to_have_attribute("inert", "")


def test_new_category_answered_after_its_dialog_was_closed_just_appears(two_logged_in_pages):
    page, _ = two_logged_in_pages
    held: list[Route] = []
    page.route(re.compile(r"/boards/\d+/categories$"), lambda route: held.append(route))
    opener = page.get_by_role("button", name="New category")
    dialog = page.get_by_role("dialog", name="New category")
    opener.click()
    dialog.get_by_label("Category name").fill("Bakery")
    dialog.get_by_label("Category name").press("Enter")
    wait_until(page, lambda: held, "the new category's request")

    dialog.get_by_role("button", name="Cancel").click()  # the answer is slow: given up on
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    held[0].continue_()

    expect(category(page, "Bakery")).to_be_visible()
    expect(opener).to_be_focused()  # the focus stays where the user left it
    expect(dialog).to_be_hidden()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")


def publish_item_event(seed, board_id: int, category_id: int, *item_ids: int) -> None:
    """Announce a change to these items, as the checklist and attachment routes do, to every
    device on the board."""

    async def publish(db):
        event = service._category_changed(board_id, category_id, item_ids=item_ids)
        await commit_and_publish(db, event)

    seed(publish)


def test_open_item_details_refresh_live_while_the_board_waits(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page

    async def make(db):
        board = await create_board(db, user, "Groceries")
        dairy = await create_category(db, board, "Dairy")
        milk = await create_item(db, dairy, "Milk")
        bread = await create_item(db, dairy, "Bread")
        return board.id, dairy.id, milk.id, bread.id

    board_id, dairy_id, milk_id, bread_id = seed(make)
    open_board(page, board_id)
    fetched: list[str] = []
    page.on("request", lambda r: fetched.append(r.url) if DETAILS.search(r.url) else None)
    page.get_by_role("button", name="Milk", exact=True).click()
    dialog = page.get_by_role("dialog")
    details = dialog.locator("#item-details")
    expect(details).to_contain_text("Modified")  # loaded
    name = dialog.get_by_label("Name")
    name.fill("Oat milk")
    fetched.clear()

    publish_item_event(seed, board_id, dairy_id, bread_id)  # not the open item
    page.wait_for_timeout(SETTLE_MS)

    assert fetched == []

    async def edit(db):
        await db.execute(
            update(Item)
            .where(Item.id == milk_id)
            .values(name="Whole milk", updated_at=datetime(2031, 5, 6, 7, 8, tzinfo=UTC))
        )
        await db.commit()

    seed(edit)
    publish_item_event(seed, board_id, dairy_id, milk_id)  # the open one

    expect(details).to_contain_text("Modified 2031-05-06 07:08 UTC", timeout=LIVE_UPDATE_MS)
    expect(name).to_have_value("Oat milk")  # what is being typed stays
    expect(name).to_be_focused()
    expect(page.locator(f"#item-{milk_id}-name")).to_have_text("Milk")  # the board waits
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("button", name="Whole milk", exact=True)).to_be_visible(
        timeout=LIVE_UPDATE_MS
    )


def set_updated_at(seed, item_id: int, when: datetime) -> None:
    async def run(db):
        await db.execute(update(Item).where(Item.id == item_id).values(updated_at=when))
        await db.commit()

    seed(run)


def modified(seed, item_id: int) -> str:
    """The "Modified" date of the item as the details show it."""
    when = seed(lambda db: db.scalar(select(Item.updated_at).where(Item.id == item_id)))
    return f"Modified {when:%Y-%m-%d %H:%M} UTC"


@pytest.mark.parametrize("change", ["tick", "rename"])
def test_open_item_details_show_a_tick_or_rename_from_another_device(
    two_logged_in_pages, seed, change
):
    a, b = two_logged_in_pages
    milk_id = seed(lambda db: db.scalar(select(Item.id)))
    set_updated_at(seed, milk_id, datetime(2030, 1, 2, 3, 4, tzinfo=UTC))
    a.get_by_role("button", name="Milk", exact=True).click()
    dialog = a.get_by_role("dialog")
    details = dialog.locator("#item-details")
    expect(details).to_contain_text("Modified 2030-01-02 03:04 UTC")
    name = dialog.get_by_label("Name")
    name.fill("Oat milk")

    if change == "tick":
        with b.expect_response(TOGGLED):
            b.get_by_role("checkbox", name="Milk").check()
    else:
        b.get_by_role("button", name="Milk", exact=True).click()
        other = b.get_by_role("dialog")
        other.get_by_label("Name").fill("Whole milk")
        with b.expect_response(re.compile(r"/items/\d+$")):
            other.get_by_role("button", name="Save").click()

    expect(details).to_contain_text(modified(seed, milk_id), timeout=LIVE_UPDATE_MS)
    expect(details).not_to_contain_text("2030-01-02")
    expect(name).to_have_value("Oat milk")  # what is being typed stays
    expect(name).to_be_focused()


def test_details_refresh_is_not_queued_behind_the_fetch_that_opened_them(
    signed_in_page, seed, user, open_board
):
    """htmx queues a request behind another one from the same element, and answers the promise
    of the queued one at once: the refresh must not come from the element the opening fetch did."""
    board_id = seed_two_categories(seed, user)
    page = signed_in_page()
    open_board(page, board_id)
    held: list[Route] = []
    page.route(DETAILS, lambda route: held.append(route))  # each details request waits for the test

    page.get_by_role("button", name="Milk", exact=True).click()
    wait_until(page, lambda: len(held) == 1, "the fetch that opens the details")
    # A tab sent to the background lets go of what it holds; back on it, the details are fetched
    # again, while the first fetch is still on its way.
    set_visibility(page, "hidden")
    set_visibility(page, "visible")

    wait_until(page, lambda: len(held) == 2, "the refresh, sent while the first fetch is held")
    for route in held:
        route.continue_()
    expect(page.locator("#item-details")).to_contain_text("Modified")


def test_open_item_details_are_fetched_once_again_after_a_reconnect(
    signed_in_page, seed, user, open_board
):
    board_id = seed_two_categories(seed, user)
    milk_id = seed(lambda db: db.scalar(select(Item.id).where(Item.name == "Milk")))
    set_updated_at(seed, milk_id, datetime(2030, 1, 2, 3, 4, tzinfo=UTC))
    page = signed_in_page()
    sockets = routed_sockets(page)
    open_board(page, board_id)
    page.get_by_role("button", name="Milk", exact=True).click()
    details = page.locator("#item-details")
    expect(details).to_contain_text("Modified 2030-01-02 03:04 UTC")
    fetched: list[str] = []
    page.on("request", lambda r: fetched.append(r.url) if DETAILS.search(r.url) else None)

    sockets[0].close(code=1012)  # the connection drops, with the item open
    expect(page.locator("#conn-status")).to_have_text("Reconnecting…")
    # A change that no event brings: made while the page was not connected.
    set_updated_at(seed, milk_id, datetime(2031, 5, 6, 7, 8, tzinfo=UTC))

    expect(details).to_contain_text("Modified 2031-05-06 07:08 UTC", timeout=5000)
    assert len(sockets) == 2
    page.wait_for_timeout(SETTLE_MS)
    assert len(fetched) == 1, fetched


def test_refresh_during_a_held_touch_drag_does_not_end_it(
    logged_in_page_mobile, seed, user, open_board
):
    board_id = seed_two_categories(seed, user)
    page = logged_in_page_mobile
    messages = received_messages(page)
    open_board(page, board_id)
    # Scrolled up out from under the phone's bottom bar, where a finger can reach it.
    item(page, "Bread").evaluate("(el) => el.scrollIntoView({ block: 'center' })")
    start, end = item(page, "Milk").bounding_box(), item(page, "Bread").bounding_box()
    touch = page.context.new_cdp_session(page)

    def finger(kind: str, fraction: float | None = None) -> None:
        points = []
        if fraction is not None:
            y = start["y"] + 10 + (end["y"] - start["y"]) * fraction
            points = [{"x": start["x"] + start["width"] / 2, "y": y}]
        touch.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    finger("touchStart", 0)
    page.wait_for_timeout(400)  # a long press: the drag starts
    for step in range(1, 6):
        finger("touchMove", step / 10)
        page.wait_for_timeout(20)
    # Meanwhile another device adds a category: the whole board is due for a reload.
    seed(lambda db: service.create_category(db, board_id, user, "Fruit"))
    wait_for_message(page, messages, "category.changed")
    page.wait_for_timeout(SETTLE_MS)
    for step in range(6, 11):
        finger("touchMove", step / 10)
        page.wait_for_timeout(20)
    page.wait_for_timeout(200)  # Sortable looks under the finger every 50 ms
    with page.expect_response(MOVED) as moved:
        finger("touchEnd")

    assert moved.value.status == 200
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    expect(category(page, "Fruit")).to_be_visible()


def test_refresh_during_a_mouse_drag_does_not_end_it(logged_in_page, seed, user, open_board):
    board_id = seed_two_categories(seed, user)
    page = logged_in_page
    messages = received_messages(page)
    open_board(page, board_id)
    bread = item(page, "Bread").bounding_box()
    x, y = centre(item(page, "Milk"))

    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 10, y + 10, steps=5)  # the drag starts
    seed(lambda db: service.create_category(db, board_id, user, "Fruit"))
    wait_for_message(page, messages, "category.changed")
    page.wait_for_timeout(SETTLE_MS)
    # Into another list Sortable inserts only off the target's centre line, as a hand does.
    page.mouse.move(bread["x"] + 20, bread["y"] + bread["height"] - 6, steps=10)
    with page.expect_response(MOVED) as moved:
        page.mouse.up()

    assert moved.value.status == 200
    expect(items_of(page, "Bakery")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    expect(category(page, "Fruit")).to_be_visible()


def test_click_in_progress_is_not_lost_to_a_remote_change(two_logged_in_pages):
    a, b = two_logged_in_pages
    milk = b.get_by_role("checkbox", name="Milk")
    b.mouse.move(*centre(milk))
    b.mouse.down()

    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)

    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")])  # held back: a click is due
    with b.expect_response(TOGGLED, timeout=LIVE_UPDATE_MS):
        b.mouse.up()
    expect(milk).to_be_checked()
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_own_tick_is_not_undone_by_a_remote_change_meanwhile(two_logged_in_pages):
    a, b = two_logged_in_pages
    held: list[Route] = []
    b.route(TOGGLED, lambda route: held.append(route))  # the tick waits here, unanswered
    milk = b.get_by_role("checkbox", name="Milk")
    milk.click()
    expect(milk).to_be_checked()
    assert held, "the tick was not sent"

    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)

    expect(milk).to_be_checked()  # not swapped back to the board from before the tick
    with b.expect_response(TOGGLED):
        held[0].continue_()
    expect(milk).to_be_checked()
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_promoted_viewer_can_open_the_item_modal_without_reloading(
    logged_in_page, signed_in_as, seed, user, open_board
):
    async def make(db):
        viewer = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", members={viewer: BoardRole.VIEWER})
        await create_item(db, await create_category(db, board, "Dairy"), "Milk")
        return board.id, viewer.id

    board_id, viewer_id = seed(make)
    viewer_page = signed_in_as("vic@example.com")
    open_board(viewer_page, board_id)
    viewer_page.get_by_role("button", name="Milk", exact=True).click()  # the read-only dialog
    expect(viewer_page.get_by_role("dialog").get_by_label("Name")).to_have_count(0)
    expect(viewer_page.get_by_role("button", name="Close")).to_be_focused()  # Esc is heard there
    viewer_page.keyboard.press("Escape")
    expect(viewer_page.get_by_role("dialog")).to_have_count(0)

    owner_page = logged_in_page
    owner_page.goto(f"/boards/{board_id}/members")
    with owner_page.expect_response(re.compile(rf"/members/{viewer_id}$")):
        owner_page.get_by_label("Role of vic@example.com").select_option("editor")

    # A viewer has a button for the name too: the page is the editor's once "Add item" is there.
    expect(viewer_page.get_by_placeholder("Add item")).to_be_visible(timeout=LIVE_UPDATE_MS)
    viewer_page.get_by_role("button", name="Milk", exact=True).click()
    expect(viewer_page.get_by_role("dialog").get_by_label("Name")).to_have_value("Milk")


def test_removed_member_is_sent_to_the_board_list(
    logged_in_page, signed_in_as, seed, user, open_board
):
    async def make(db):
        viewer = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", members={viewer: BoardRole.VIEWER})
        return board.id

    board_id = seed(make)
    viewer_page = signed_in_as("vic@example.com")
    open_board(viewer_page, board_id)
    reconnects = []
    viewer_page.on("websocket", lambda ws: reconnects.append(ws.url))

    owner_page = logged_in_page
    owner_page.goto(f"/boards/{board_id}/members")
    owner_page.once("dialog", lambda dialog: dialog.accept())
    owner_page.get_by_role("button", name="Remove").click()

    expect(viewer_page).to_have_url(re.compile(r"/boards$"), timeout=LIVE_UPDATE_MS)
    expect(viewer_page.get_by_text("You have no boards yet.")).to_be_visible()
    assert reconnects == []  # straight to the board list, without trying the socket again


def test_member_removed_while_disconnected_is_sent_to_the_board_list(
    signed_in_as, seed, user, open_board
):
    async def make(db):
        viewer = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", members={viewer: BoardRole.VIEWER})
        return board.id, viewer.id

    board_id, viewer_id = seed(make)
    page = signed_in_as("vic@example.com")
    sockets = routed_sockets(page)
    open_board(page, board_id)

    sockets[0].close(code=1012)  # the connection drops
    expect(page.locator("#conn-status")).to_have_text("Reconnecting…")

    async def remove(db):  # no event reaches the page: it is not connected
        await db.execute(
            delete(BoardMember).where(
                BoardMember.board_id == board_id, BoardMember.user_id == viewer_id
            )
        )
        await db.commit()

    seed(remove)

    # The reconnect is refused (4404), and the page leaves the board instead of retrying.
    expect(page).to_have_url(re.compile(r"/boards$"), timeout=5000)
    assert len(sockets) == 2


def test_signed_out_device_is_sent_to_the_login_page(two_logged_in_pages):
    a, b = two_logged_in_pages
    reconnects = []
    b.on("websocket", lambda ws: reconnects.append(ws.url))

    a.goto("/account")
    a.get_by_role("button", name="Sign out other devices").click()

    expect(b).to_have_url(re.compile(r"/login$"), timeout=LIVE_UPDATE_MS)
    assert reconnects == []  # straight to the login page, without trying the socket again


def test_dropped_connection_reconnects_and_catches_up(signed_in_page, seed, user, open_board):
    async def make(db):
        board = await create_board(db, user, "Groceries")
        dairy = await create_category(db, board, "Dairy")
        await create_item(db, dairy, "Milk")
        return board.id, dairy.id

    board_id, dairy_id = seed(make)
    page = signed_in_page()
    sockets = routed_sockets(page)
    open_board(page, board_id)
    status = page.locator("#conn-status")

    sockets[0].close(code=1012)  # the connection drops
    expect(status).to_have_text("Reconnecting…")
    # A change that no event brings: made while the page was not connected.
    seed(lambda db: service.create_item(db, dairy_id, user, "Bread"))

    expect(status).to_have_text("Live", timeout=5000)  # the first retry comes after about 1 s
    assert len(sockets) == 2
    expect(items_of(page, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])


def test_cursor_left_in_an_empty_field_does_not_hold_updates(two_logged_in_pages, seed, user):
    a, b = two_logged_in_pages
    add_item(b, "Dairy", "Eggs")  # the field is emptied, the cursor stays in it
    field = category(b, "Dairy").get_by_placeholder("Add item")
    expect(field).to_be_focused()
    expect(field).to_have_value("")
    board_id = int(b.locator("#board-content").get_attribute("data-board-id"))

    add_item(a, "Dairy", "Bread")
    with a.expect_response(TOGGLED):
        a.get_by_role("checkbox", name="Milk").click()
    seed(lambda db: service.create_category(db, board_id, user, "Drinks"))

    expect(b.get_by_role("checkbox", name="Bread")).to_be_visible(timeout=LIVE_UPDATE_MS)
    expect(b.get_by_role("checkbox", name="Milk")).to_be_checked(timeout=LIVE_UPDATE_MS)
    expect(category(b, "Drinks")).to_be_visible(timeout=LIVE_UPDATE_MS)
    expect(field).to_be_focused()  # still ready for the next item


def test_back_on_the_tab_it_catches_up_with_the_cursor_in_an_empty_field(two_logged_in_pages, seed):
    _, b = two_logged_in_pages
    add_item(b, "Dairy", "Eggs")
    field = category(b, "Dairy").get_by_placeholder("Add item")
    expect(field).to_be_focused()
    dairy_id = int(category(b, "Dairy").get_attribute("data-category-id"))
    set_visibility(b, "hidden")

    async def no_event(db):  # written straight to the database: only the catch-up brings it
        await create_item(db, await db.get(Category, dairy_id), "Bread")

    seed(no_event)
    set_visibility(b, "visible")

    expect(b.get_by_role("checkbox", name="Bread")).to_be_visible(timeout=LIVE_UPDATE_MS)
    expect(field).to_be_focused()


def test_archived_category_leaves_and_comes_back_on_the_other_device(two_logged_in_pages):
    a, b = two_logged_in_pages
    b.evaluate("window.notReloaded = true")
    reloads = board_reloads(b)

    category(a, "Dairy").get_by_label("Category actions").click()
    a.once("dialog", lambda dialog: dialog.accept())
    category(a, "Dairy").get_by_role("button", name="Archive category").click()

    expect(category(b, "Dairy")).to_have_count(0, timeout=LIVE_UPDATE_MS)
    assert board_reloads(b) == reloads  # only that section went, the board was not fetched again

    a.get_by_role("link", name="Archived categories").click()
    a.get_by_role("button", name="Restore").click()

    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")], timeout=LIVE_UPDATE_MS)
    assert b.evaluate("window.notReloaded") is True  # the page itself was never reloaded
    assert b.locator("#toasts > *").all_inner_texts() == []


def test_moved_category_leaves_one_board_and_arrives_on_the_other(
    signed_in_page, signed_in_as, seed, user, open_board
):
    async def make(db):
        vic = await create_user(db, "vic@example.com")
        groceries = await create_board(db, user, "Groceries")
        await create_item(db, await create_category(db, groceries, "Dairy"), "Milk")
        weekend = await create_board(db, user, "Weekend", members={vic: BoardRole.VIEWER})
        await create_category(db, weekend, "Bakery")
        return groceries.id, weekend.id

    groceries_id, weekend_id = seed(make)
    a, b, c = signed_in_page(), signed_in_page(), signed_in_as("vic@example.com")
    for page, board_id in ((a, groceries_id), (b, groceries_id), (c, weekend_id)):
        open_board(page, board_id)
    b.evaluate("window.notReloaded = true")
    reloads = board_reloads(b)

    category(a, "Dairy").get_by_label("Category actions").click()
    category(a, "Dairy").get_by_role("button", name="Move to board…").click()
    dialog = a.get_by_role("dialog", name="Move to board")
    dialog.get_by_role("button", name="Move", exact=True).click()  # Weekend, still "Dairy"

    expect(category(b, "Dairy")).to_have_count(0, timeout=LIVE_UPDATE_MS)
    assert board_reloads(b) == reloads  # only that section went, the board was not fetched again
    assert b.evaluate("window.notReloaded") is True
    assert b.locator("#toasts > *").all_inner_texts() == []
    expect(c.locator("section[data-category-id] h2")).to_have_text(
        ["Bakery", "Dairy"], timeout=LIVE_UPDATE_MS
    )
    expect(items_of(c, "Dairy")).to_have_text([re.compile("Milk")])
    expect(a.locator("#toasts")).to_have_text("Moved to “Weekend”.")


def test_text_typed_into_add_item_survives_changes_to_its_category(two_logged_in_pages):
    a, b = two_logged_in_pages
    field = category(b, "Dairy").get_by_placeholder("Add item")
    field.fill("Chee")
    b.get_by_role("heading", level=1).click()  # looked away: the field keeps its text

    add_item(a, "Dairy", "Bread")

    expect(items_of(b, "Dairy")).to_have_text(
        [re.compile("Bread"), re.compile("Milk")], timeout=LIVE_UPDATE_MS
    )
    expect(field).to_have_value("Chee")
    with b.expect_response(TOGGLED):  # its own tick's answer replaces the category too
        b.get_by_role("checkbox", name="Milk").click()
    expect(b.get_by_role("checkbox", name="Milk")).to_be_checked()
    expect(field).to_have_value("Chee")


def test_late_answer_to_a_board_reload_does_not_undo_an_own_tick(two_logged_in_pages, seed, user):
    _, b = two_logged_in_pages
    held = []

    def answer_late(route: Route) -> None:
        if held:
            route.continue_()
        else:  # the board as it was before the tick, answered only after it
            held.append((route, route.fetch()))

    b.route(re.compile(r"/boards/\d+/content$"), answer_late)
    board_id = int(b.locator("#board-content").get_attribute("data-board-id"))
    seed(lambda db: service.create_category(db, board_id, user, "Bakery"))  # a board reload
    wait_until(b, lambda: held, "the board reload's request")
    milk = b.get_by_role("checkbox", name="Milk")
    with b.expect_response(TOGGLED):
        milk.click()
    expect(milk).to_be_checked()

    route, response = held[0]
    route.fulfill(response=response)
    b.wait_for_timeout(SETTLE_MS)

    expect(milk).to_be_checked()  # the stale answer was dropped and the board fetched again
    expect(category(b, "Bakery")).to_be_visible()


def test_update_finding_its_category_gone_does_not_reload_the_board_while_typing(
    signed_in_page, seed, user, open_board
):
    board_id = seed_two_categories(seed, user)
    page = signed_in_page()
    messages = received_messages(page)
    open_board(page, board_id)
    dairy_id = int(category(page, "Dairy").get_attribute("data-category-id"))
    held: list[Route] = []
    page.route(
        re.compile(r"/categories/\d+$"),
        lambda route: route.continue_() if held else held.append(route),
    )
    seed(lambda db: service.create_item(db, dairy_id, user, "Butter"))
    wait_until(page, lambda: held, "the update's request")
    seed(lambda db: service.archive_category(db, dairy_id, user))
    wait_for_message(page, messages, "category.deleted")
    field = category(page, "Bakery").get_by_placeholder("Add item")
    field.fill("Chee")
    reloads = board_reloads(page)

    held[0].continue_()  # answered 404, which asks for a board reload ("resync")
    page.wait_for_timeout(SETTLE_MS)

    expect(field).to_have_value("Chee")
    expect(field).to_be_focused()
    assert board_reloads(page) == reloads  # waits for the typing like any update
    expect(category(page, "Dairy")).to_have_count(0)


def test_update_that_could_not_be_fetched_shows_no_error(two_logged_in_pages):
    a, b = two_logged_in_pages
    aborted = []

    def abort(route: Route) -> None:
        aborted.append(route.request.url)
        route.abort()

    b.route(re.compile(r"/categories/\d+$"), abort)
    add_item(a, "Dairy", "Bread")
    wait_until(b, lambda: aborted, "the update's request")
    b.wait_for_timeout(SETTLE_MS)

    assert (
        b.locator("#toasts > *").all_inner_texts() == []
    )  # the user did nothing: the header says it


def test_role_change_reload_waits_for_the_item_modal(
    logged_in_page, signed_in_as, seed, user, open_board
):
    async def make(db):
        editor = await create_user(db, "ed@example.com")
        board = await create_board(db, user, "Groceries", members={editor: BoardRole.EDITOR})
        await create_item(db, await create_category(db, board, "Dairy"), "Milk")
        return board.id, editor.id

    board_id, editor_id = seed(make)
    page = signed_in_as("ed@example.com")
    open_board(page, board_id)
    page.evaluate("window.notReloaded = true")
    page.get_by_role("button", name="Milk", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name").fill("Oat milk")

    owner_page = logged_in_page
    owner_page.goto(f"/boards/{board_id}/members")
    with owner_page.expect_response(re.compile(rf"/members/{editor_id}$")):
        owner_page.get_by_label("Role of ed@example.com").select_option("viewer")
    page.wait_for_timeout(SETTLE_MS)

    assert page.evaluate("window.notReloaded") is True  # not while the modal is open
    expect(dialog.get_by_label("Name")).to_have_value("Oat milk")
    dialog.get_by_role("button", name="Cancel").click()
    # Reloaded as a viewer: no "Add item" any more (the name's button is there for a viewer too).
    expect(page.get_by_placeholder("Add item")).to_have_count(0, timeout=LIVE_UPDATE_MS)
    assert page.evaluate("window.notReloaded") is None


def test_button_held_when_the_tab_was_hidden_does_not_hold_updates(two_logged_in_pages):
    a, b = two_logged_in_pages
    b.mouse.move(*centre(b.get_by_role("heading", level=1)))
    b.mouse.down()  # its pointerup is lost: the tab goes to the background meanwhile
    add_item(a, "Dairy", "Bread")
    b.wait_for_timeout(SETTLE_MS)
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Milk")])  # held: a click is due

    set_visibility(b, "hidden")
    set_visibility(b, "visible")

    expect(items_of(b, "Dairy")).to_have_text(
        [re.compile("Bread"), re.compile("Milk")], timeout=LIVE_UPDATE_MS
    )
    b.mouse.up()


def test_long_time_away_replaces_the_socket(signed_in_page, seed, user, open_board):
    board_id = seed_two_categories(seed, user)
    page = signed_in_page()
    sockets = routed_sockets(page)
    open_board(page, board_id)

    set_visibility(page, "hidden")
    # Half a minute later (longer than the server's ping interval): the socket may be dead.
    page.evaluate(
        "() => { const now = performance.now.bind(performance); performance.now = () => now() + 30000; }"
    )
    set_visibility(page, "visible")

    wait_until(page, lambda: len(sockets) == 2, "a new socket")
    expect(page.locator("#conn-status")).to_have_text("Live")


def test_socket_refused_at_the_wrong_address_says_where_to_open_the_app(
    signed_in_page, seed, user, open_board, live_server
):
    board_id = seed_two_categories(seed, user)
    page = signed_in_page()
    sockets = routed_sockets(page)
    open_board(page, board_id)

    sockets[0].close(code=4400)  # as the server answers an Origin other than the public URL

    expect(page.locator("#conn-status")).to_have_text("Offline")
    expect(page.locator("#toasts")).to_have_text(
        "Live updates and saving do not work at this address. "
        f"Open EasyToDo at {live_server.settings.public_origin}."
    )
    page.wait_for_timeout(SETTLE_MS)
    assert page.url.endswith(f"/boards/{board_id}")  # stays, not sent to the board list
    assert len(sockets) == 1  # and not retried


def test_sort_order_is_personal_on_a_shared_board(
    logged_in_page, signed_in_as, seed, user, open_board
):
    async def make(db):
        vic = await create_user(db, "vic@example.com")
        board = await create_board(db, user, "Groceries", members={vic: BoardRole.EDITOR})
        dairy = await create_category(db, board, "Dairy")
        await create_item(db, dairy, "Butter")
        await create_item(db, dairy, "Milk")
        return board.id

    board_id = seed(make)
    dave, vic = logged_in_page, signed_in_as("vic@example.com")
    for page in (dave, vic):
        open_board(page, board_id)

    dairy_sort = category(dave, "Dairy").get_by_label("Sort Dairy")
    dairy_sort.click()
    category(dave, "Dairy").get_by_role("radio", name="Z–A / newest first", exact=True).check()
    category(dave, "Dairy").get_by_role("button", name="Apply", exact=True).click()
    expect(items_of(dave, "Dairy")).to_have_text([re.compile("Milk"), re.compile("Butter")])
    vic.wait_for_timeout(SETTLE_MS)
    expect(items_of(vic, "Dairy")).to_have_text([re.compile("Butter"), re.compile("Milk")])

    add_item(vic, "Dairy", "Cheese")  # each gets it in the order they chose
    expect(items_of(vic, "Dairy")).to_have_text(
        [re.compile("Butter"), re.compile("Cheese"), re.compile("Milk")]
    )
    expect(items_of(dave, "Dairy")).to_have_text(
        [re.compile("Milk"), re.compile("Cheese"), re.compile("Butter")], timeout=LIVE_UPDATE_MS
    )

    category(vic, "Dairy").get_by_label("Sort Dairy").click()
    add_item(dave, "Dairy", "Cream")
    vic.wait_for_timeout(SETTLE_MS)
    expect(category(vic, "Dairy").get_by_role("button", name="Apply")).to_be_visible()  # still open
    expect(items_of(vic, "Dairy").filter(has_text="Cream")).to_have_count(0)

    vic.keyboard.press("Escape")

    expect(items_of(vic, "Dairy")).to_have_text(
        [re.compile(name) for name in ("Butter", "Cheese", "Cream", "Milk")],
        timeout=LIVE_UPDATE_MS,
    )
