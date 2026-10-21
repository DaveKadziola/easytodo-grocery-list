"""Live board updates in Chromium: changes on one device show on the others without reloading,
without getting in the way of what the user is doing there (typing, the item modal, a click or
a drag in progress, a request of its own), and the page follows the server when it signs the
device out or the member is removed; after a dropped connection it reconnects and catches up."""

import json
import re
from collections.abc import Callable

import pytest
from factories import create_board, create_category, create_item, create_user
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect
from sqlalchemy import delete

from easytodo.content import service
from easytodo.models import BoardMember, BoardRole

pytestmark = pytest.mark.e2e

LIVE_UPDATE_MS = 2000  # how soon a change must show on the other device
SETTLE_MS = 1000  # long enough for an update that is (wrongly) not held back to show
MOVED = re.compile(r"/items/\d+/move$")
TOGGLED = re.compile(r"/items/\d+/toggle$")


def category(page: Page, name: str) -> Locator:
    return page.locator("section[data-category-id]").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )


def items_of(page: Page, category_name: str) -> Locator:
    return category(page, category_name).locator("li[data-item-id]")


def item(page: Page, name: str) -> Locator:
    return page.locator("li[data-item-id]").filter(has_text=re.compile(rf"^\s*{name}\s*$"))


def add_item(page: Page, category_name: str, name: str) -> None:
    section = category(page, category_name)
    section.get_by_placeholder("Add item").fill(name)
    section.get_by_role("button", name="Add", exact=True).click()
    expect(items_of(page, category_name).filter(has_text=name)).to_have_count(1)


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

    a.get_by_label("Board name").fill("Weekend")
    a.locator("form", has=a.get_by_label("Board name")).get_by_role("button", name="Rename").click()

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


def test_refresh_during_a_held_touch_drag_does_not_end_it(
    logged_in_page_mobile, seed, user, open_board
):
    board_id = seed_two_categories(seed, user)
    page = logged_in_page_mobile
    messages = received_messages(page)
    open_board(page, board_id)
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
    expect(viewer_page.get_by_role("button", name="Milk", exact=True)).to_have_count(0)

    owner_page = logged_in_page
    owner_page.goto(f"/boards/{board_id}/members")
    with owner_page.expect_response(re.compile(rf"/members/{viewer_id}$")):
        owner_page.get_by_label("Role of vic@example.com").select_option("editor")

    viewer_page.get_by_role("button", name="Milk", exact=True).click(timeout=LIVE_UPDATE_MS)
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
