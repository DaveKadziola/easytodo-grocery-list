"""Coming back after missing changes, and changes that do not go through: a device that lost its
connection catches up as soon as it is back (not at its next reconnect attempt), a tick shows at
once and is undone, with a toast, when the server does not take it, and a live update does not
replace what the user is typing in the item modal."""

import re
import time

import pytest
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect

pytestmark = pytest.mark.e2e

BOARD_SOCKET = re.compile(r"/ws/boards/\d+")
TOGGLED = re.compile(r"/items/\d+/toggle$")
# After a dropped connection realtime.js tries again in 1 s ±20 %.
FIRST_RETRY_EARLIEST_S = 0.8
FIRST_RETRY_LATEST_S = 1.2
SETTLE_MS = 1000  # long enough for an update that is (wrongly) not held back to show
STRUCK = re.compile(r"\bline-through\b")


def category(page: Page, name: str) -> Locator:
    return page.locator("section[data-category-id]").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )


def items_of(page: Page, category_name: str) -> Locator:
    return category(page, category_name).locator("li[data-item-id]")


def item(page: Page, name: str) -> Locator:
    return page.locator("li[data-item-id]").filter(
        has=page.get_by_role("checkbox", name=name, exact=True)
    )


def add_item(page: Page, category_name: str, name: str) -> None:
    section = category(page, category_name)
    section.get_by_placeholder("Add item").fill(name)
    section.get_by_role("button", name="Add", exact=True).click()
    expect(items_of(page, category_name).filter(has_text=name)).to_have_count(1)


def expect_struck(row: Locator, struck: bool) -> None:
    if struck:
        expect(row).to_have_class(STRUCK)
    else:
        expect(row).not_to_have_class(STRUCK)


def wait_until(page: Page, condition, what: str) -> None:
    """Wait (up to 2 s) until ``condition()`` holds, letting ``page``'s events come in."""
    for _ in range(40):
        if condition():
            return
        page.wait_for_timeout(50)
    pytest.fail(f"{what} did not happen")


@pytest.mark.parametrize("comeback", ["online", "visibilitychange"])
def test_offline_device_resyncs_after_reconnect(two_logged_in_pages, open_board, comeback):
    """Review Focus 3: a phone that lost its signal ("online" when it is back) or slept with the
    screen off ("visibilitychange" when it wakes) shows the changes it missed straight away, not
    at its next reconnect attempt."""
    a, b = two_logged_in_pages
    attempts: list[float] = []  # when b opened the board's socket
    sockets: list[WebSocketRoute] = []
    missed: list[str] = []
    away = [False]

    def connect(ws: WebSocketRoute) -> None:
        attempts.append(time.monotonic())
        sockets.append(ws)
        server = ws.connect_to_server()
        # While b is away, nothing the server sends reaches it.
        server.on_message(lambda message: missed.append(message) if away[0] else ws.send(message))

    b.route_web_socket(BOARD_SOCKET, connect)
    open_board(b, int(b.locator("#board-content").get_attribute("data-board-id")))
    status = b.locator("#conn-status")

    away[0] = True
    add_item(a, "Dairy", "Bread")
    wait_until(b, lambda: missed, "the missed event")
    dropped = time.monotonic()
    sockets[0].close(code=1006)  # the connection is really gone (set_offline may not drop it)
    if comeback == "online":
        b.context.set_offline(True)
    expect(status).to_have_text("Reconnecting…")
    assert len(attempts) == 1

    away[0] = False
    back = time.monotonic()
    if comeback == "online":
        b.context.set_offline(False)  # the browser fires "online"
    else:
        b.dispatch_event("body", "visibilitychange")  # back on the screen

    wait_until(b, lambda: len(attempts) == 2, "a reconnect")
    assert attempts[1] - back < 0.5
    assert attempts[1] < dropped + FIRST_RETRY_EARLIEST_S  # sooner than the scheduled retry
    expect(items_of(b, "Dairy")).to_have_text([re.compile("Bread"), re.compile("Milk")])
    expect(status).to_have_text("Live")
    b.wait_for_timeout(max(0, dropped + FIRST_RETRY_LATEST_S - time.monotonic()) * 1000)
    assert len(attempts) == 2  # and that retry was called off


@pytest.mark.parametrize(
    ("name", "failure", "message"),
    [
        ("Yogurt", "abort", "Could not reach the server. Try again."),
        ("Milk", "abort", "Could not reach the server. Try again."),  # unticking a done item
        ("Yogurt", "500", "Something went wrong. Try again."),
    ],
)
def test_optimistic_toggle_reverts_on_error(logged_in_page_with_seed, name, failure, message):
    page = logged_in_page_with_seed
    held: list[Route] = []
    page.route(TOGGLED, lambda route: held.append(route))  # the tick waits here, unanswered
    checkbox = page.get_by_role("checkbox", name=name, exact=True)
    was_done = checkbox.is_checked()

    checkbox.click()

    expect_struck(item(page, name), not was_done)  # at once, before any answer
    assert held, "the tick was not sent"
    if failure == "abort":
        held[0].abort()
    else:
        held[0].fulfill(status=500, body="Internal Server Error")

    expect(page.locator("#toasts")).to_have_text(message)
    expect_struck(item(page, name), was_done)
    expect(checkbox).to_be_checked(checked=was_done)


def test_open_modal_is_not_clobbered_by_remote_update(two_logged_in_pages):
    a, b = two_logged_in_pages
    add_item(a, "Dairy", "Bread")
    expect(items_of(b, "Dairy")).to_have_count(2)
    b.get_by_role("button", name="Milk", exact=True).click()
    dialog = b.get_by_role("dialog")
    name = dialog.get_by_label("Name")
    name.fill("Oat milk")

    a.get_by_role("checkbox", name="Bread").click()  # another item, on the other device
    expect(a.get_by_role("checkbox", name="Bread")).to_be_checked()
    b.wait_for_timeout(SETTLE_MS)

    expect(dialog).to_be_visible()
    expect(name).to_have_value("Oat milk")
    expect(name).to_be_focused()
    dialog.get_by_role("button", name="Save").click()
    expect(b.get_by_role("checkbox", name="Oat milk")).to_be_visible()
    expect(b.get_by_role("checkbox", name="Bread")).to_be_checked()  # caught up once closed
