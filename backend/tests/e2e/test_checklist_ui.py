"""The checklist in the item's modal in Chromium: entries added with Enter, ticked, deleted and
dragged into a new order, the count on the item's tile (on this device when the modal closes, on
the other one live), and a live update that waits for a drag instead of ending it."""

import re

import pytest
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from playwright.sync_api import Page, Route, expect
from sqlalchemy import delete, select

from e2e.conftest import watch_console
from e2e.locators import item
from e2e.test_board_ui import seed_board
from e2e.test_realtime import (
    DETAILS,
    LIVE_UPDATE_MS,
    SETTLE_MS,
    centre,
    received_messages,
    wait_for_message,
    wait_until,
)
from e2e.test_responsive import AA, LOW_CONTRAST_TEXT, at_least_44px, too_small_targets
from easytodo.content.checklist import check_entry
from easytodo.models import BoardRole, ChecklistEntry, Item

pytestmark = pytest.mark.e2e

CHECKED = re.compile(r"/checklist/\d+/check$")
ORDERED = re.compile(r"/checklist/order$")
GREEN = re.compile(r"\btext-emerald-700\b")


def add_entries(seed, item_name: str, *texts: str) -> list[int]:
    """Entries with these texts, in this order, on the item ``item_name``; returns their ids."""

    async def make(db) -> list[int]:
        found = await db.scalar(select(Item).where(Item.name == item_name))
        return [
            (await create_checklist_entry(db, found, text, position=position)).id
            for position, text in enumerate(texts)
        ]

    return seed(make)


def open_item(page: Page, name: str):
    """Open the modal (or the read-only dialog) of the item ``name``; returns it, with its
    checklist loaded."""
    page.get_by_role("button", name=name, exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("#item-checklist")).to_be_visible()
    return dialog


def expect_entries(dialog, *texts: str) -> None:
    """The checklist in the open modal has exactly these entries, in this order (waits for them)."""
    rows = dialog.locator("li[data-entry-id]")
    expect(rows).to_have_count(len(texts))
    for position, text in enumerate(texts):
        expect(rows.nth(position).locator("input[name=text]")).to_have_value(text)


def tile_count(page: Page, name: str):
    """The checklist count in the tile of the item ``name``."""
    return item(page, name).locator(".checklist-count")


def test_checklist_is_added_ticked_and_counted_on_the_tile(
    logged_in_page_mobile, seed, user, open_board
):
    page = logged_in_page_mobile
    problems = watch_console(page)
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)

    item(page, "Milk").get_by_role("button", name="Milk", exact=True).tap()
    dialog = page.get_by_role("dialog")
    new_entry = dialog.get_by_label("Add entry")
    for text in ("Eggs", "Flour", "Sugar", "Salt"):
        new_entry.fill(text)
        new_entry.press("Enter")
        expect(dialog.get_by_label(f"Edit {text}")).to_have_value(text)
        expect(new_entry).to_have_value("")  # ready for the next one
        expect(new_entry).to_be_focused()
    count = dialog.locator(".checklist-count")
    expect(count).to_have_text("0/4")
    assert too_small_targets(page) == []  # the whole page, the modal's controls too
    at_least_44px(dialog.locator(".checklist-handle"))

    dialog.get_by_role("button", name="Delete Salt").tap()
    expect(dialog.get_by_label("Edit Salt")).to_have_count(0)
    expect(new_entry).to_be_focused()
    expect(count).to_have_text("0/3")

    for text in ("Eggs", "Flour"):
        with page.expect_response(CHECKED) as ticked:
            dialog.get_by_role("checkbox", name=text).tap()
        assert ticked.value.status == 200
        expect(dialog.get_by_role("checkbox", name=text)).to_be_checked()
    expect(count).to_have_text("2/3")

    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(tile_count(page, "Milk")).to_have_text("2/3")  # the tile follows the closed modal
    expect(item(page, "Milk").get_by_role("checkbox", name="Milk")).not_to_be_checked()  # P10

    item(page, "Milk").get_by_role("button", name="Milk", exact=True).tap()
    with page.expect_response(CHECKED):
        dialog.get_by_role("checkbox", name="Sugar").tap()
    expect(count).to_have_text("3/3")
    expect(count).to_have_class(GREEN)
    page.keyboard.press("Escape")

    expect(tile_count(page, "Milk")).to_have_text("3/3")
    expect(tile_count(page, "Milk")).to_have_class(GREEN)
    expect(item(page, "Milk").get_by_role("checkbox", name="Milk")).not_to_be_checked()
    assert problems == []


def test_checklist_ticks_show_live_on_the_other_device(two_logged_in_pages, seed):
    a, b = two_logged_in_pages
    add_entries(seed, "Milk", "Eggs", "Flour")
    dialog_a, dialog_b = open_item(a, "Milk"), open_item(b, "Milk")

    with a.expect_response(CHECKED):
        dialog_a.get_by_role("checkbox", name="Eggs").check()

    expect(dialog_b.get_by_role("checkbox", name="Eggs")).to_be_checked(timeout=LIVE_UPDATE_MS)
    expect(dialog_b.locator(".checklist-count")).to_have_text("1/2")
    expect(dialog_b).to_be_visible()  # the modal stays open
    for page in (a, b):
        page.keyboard.press("Escape")
        expect(tile_count(page, "Milk")).to_have_text("1/2")


def test_checklist_drag_reorders_and_survives_a_live_update(logged_in_page, seed, user, open_board):
    page = logged_in_page
    messages = received_messages(page)
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    eggs, _flour, _sugar = add_entries(seed, "Milk", "Eggs", "Flour", "Sugar")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")

    expect_entries(dialog, "Eggs", "Flour", "Sugar")
    x, y = centre(entry_handle(page, "Sugar"))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + 10, steps=5)  # the drag starts
    # Meanwhile another device ticks Eggs: the details are due, but not while the entry is held.
    seed(lambda db: check_entry(db, eggs, user))
    wait_for_message(page, messages, "category.changed")
    page.wait_for_timeout(SETTLE_MS)
    expect(dialog.get_by_role("checkbox", name="Eggs")).not_to_be_checked()
    top_of_eggs = entry_handle(page, "Eggs").bounding_box()
    page.mouse.move(
        top_of_eggs["x"] + top_of_eggs["width"] / 2,
        top_of_eggs["y"] + top_of_eggs["height"] / 4,
        steps=10,
    )
    page.wait_for_timeout(200)  # Sortable looks under the pointer every 50 ms
    with page.expect_response(ORDERED) as ordered:
        page.mouse.up()

    assert ordered.value.status == 200
    expect(dialog.get_by_role("checkbox", name="Eggs")).to_be_checked(timeout=LIVE_UPDATE_MS)
    expect_entries(dialog, "Sugar", "Eggs", "Flour")

    open_board(page, board_id)  # the page again, from the server
    dialog = open_item(page, "Milk")
    expect_entries(dialog, "Sugar", "Eggs", "Flour")
    expect(dialog.get_by_role("checkbox", name="Eggs")).to_be_checked()


def entry_handle(page: Page, text: str):
    """The drag handle of the entry ``text`` in the open modal."""
    row = page.get_by_role("dialog").locator("li[data-entry-id]")
    return row.filter(has=page.get_by_label(f"Edit {text}")).locator(".checklist-handle")


@pytest.mark.parametrize("failure", ["error", "no answer"])
def test_checklist_drag_that_did_not_go_through_puts_the_entry_back(
    logged_in_page, seed, user, open_board, failure
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Eggs", "Flour")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    page.route(
        ORDERED,
        lambda route: (
            route.fulfill(status=500, body="boom") if failure == "error" else route.abort()
        ),
    )

    with page.expect_request(ORDERED):
        entry_handle(page, "Flour").drag_to(entry_handle(page, "Eggs"))

    expect_entries(dialog, "Eggs", "Flour")  # the details were fetched again


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("abort", "Could not reach the server. Try again."),
        ("500", "Something went wrong. Try again."),
    ],
)
def test_checklist_tick_that_did_not_go_through_is_undone(
    logged_in_page, seed, user, open_board, failure, message
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Eggs")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    held = []
    page.route(CHECKED, lambda route: held.append(route))  # the tick waits here, unanswered
    eggs = dialog.get_by_role("checkbox", name="Eggs")

    eggs.check()

    assert held, "the tick was not sent"
    if failure == "abort":
        held[0].abort()
    else:
        held[0].fulfill(status=500, body="Internal Server Error")
    expect(page.locator("#toasts")).to_have_text(message)
    expect(eggs).not_to_be_checked()
    expect(dialog.locator(".checklist-count")).to_have_text("0/1")


def test_a_second_tap_before_the_answer_is_not_lost(logged_in_page, seed, user, open_board):
    """Tick, then untick before the server has answered the tick: the answer replaces the checkbox,
    and the state its user wants last is sent from the new one (as for the items on the board)."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Eggs")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    held: list[Route] = []
    page.route(CHECKED, lambda route: held.append(route))
    eggs = dialog.get_by_role("checkbox", name="Eggs")

    eggs.check()
    eggs.uncheck()  # while the first one is on its way
    held.pop().continue_()  # the tick is answered: the details come back with it ticked
    wait_until(page, lambda: held, "the untick, sent from the new checkbox")
    held.pop().continue_()

    expect(eggs).not_to_be_checked()
    expect(dialog.locator(".checklist-count")).to_have_text("0/1")
    assert not held


def test_a_checklist_entry_deleted_on_another_device_leaves_the_details(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    eggs, _flour = add_entries(seed, "Milk", "Eggs", "Flour")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")

    async def delete_without_a_word(db):  # no event: the page does not know
        await db.execute(delete(ChecklistEntry).where(ChecklistEntry.id == eggs))
        await db.commit()

    seed(delete_without_a_word)
    dialog.get_by_role("checkbox", name="Eggs").check()

    expect(page.locator("#toasts")).to_have_text("This checklist entry no longer exists.")
    expect(dialog.get_by_label("Edit Eggs")).to_have_count(0)  # the details were fetched again
    expect(dialog.get_by_label("Edit Flour")).to_have_count(1)
    expect(dialog.locator(".checklist-count")).to_have_text("0/1")


def test_a_checklist_of_an_item_deleted_on_another_device_says_so_once(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Eggs")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    fetched: list[str] = []
    page.on("request", lambda r: fetched.append(r.url) if DETAILS.search(r.url) else None)

    async def delete_without_a_word(db):
        await db.execute(delete(Item).where(Item.name == "Milk"))
        await db.commit()

    seed(delete_without_a_word)
    dialog.get_by_role("checkbox", name="Eggs").check()

    # The tick finds its entry gone; the details fetched again then find the item gone.
    expect(page.locator("#toasts")).to_contain_text("This item no longer exists.")
    page.wait_for_timeout(SETTLE_MS)
    assert len(fetched) == 1, fetched  # and do not ask again for what is not there


def test_a_rename_is_saved_with_enter_and_unsaved_text_stays_until_the_modal_closes(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Eggs", "Flour")
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    eggs = dialog.get_by_label("Edit Eggs")

    eggs.fill("Free-range eggs")  # typed, not saved: only Enter saves
    with page.expect_response(CHECKED):
        dialog.get_by_role("checkbox", name="Flour").check()  # the details are swapped meanwhile

    expect(dialog.get_by_role("checkbox", name="Flour")).to_be_checked()
    expect(eggs).to_have_value("Free-range eggs")  # kept across the swap
    page.keyboard.press("Escape")
    dialog = open_item(page, "Milk")
    expect(dialog.get_by_label("Edit Eggs")).to_have_value("Eggs")  # but not across the closing

    eggs = dialog.get_by_label("Edit Eggs")
    eggs.fill("  Free-range   eggs ")
    with page.expect_response(re.compile(r"/checklist/\d+$")) as renamed:
        eggs.press("Enter")
    assert renamed.value.status == 200
    expect(dialog.get_by_label("Edit Free-range eggs")).to_have_value("Free-range eggs")
    expect(dialog.get_by_role("checkbox", name="Free-range eggs")).to_be_visible()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_checklist_and_its_count_stay_readable_for_an_editor_and_a_viewer(
    signed_in_as, seed, user, open_board, scheme
):
    async def make(db) -> int:
        viewer = await create_user(db, "viewer@example.com")
        board = await create_board(db, user, "Groceries", {viewer: BoardRole.VIEWER})
        dairy = await create_category(db, board, "Dairy")
        milk = await create_item(db, dairy, "Milk", created_by=user)
        bread = await create_item(
            db, dairy, "Bread", is_done=True, created_by=user
        )  # struck through
        for owner, steps in (
            (milk, ((True, "Eggs"), (False, "Flour"))),
            (bread, ((True, "Yeast"),)),
        ):
            for position, (done, text) in enumerate(steps):
                await create_checklist_entry(db, owner, text, is_done=done, position=position)
        return board.id

    board_id = seed(make)

    for email in (user.email, "viewer@example.com"):
        page = signed_in_as(email, color_scheme=scheme)
        open_board(page, board_id)
        expect(tile_count(page, "Milk")).to_have_text("1/2")  # slate
        expect(tile_count(page, "Bread")).to_have_text("1/1")  # green, in a struck-through row
        assert page.evaluate(LOW_CONTRAST_TEXT, AA) == [], email

        dialog = open_item(page, "Milk")
        expect(dialog.locator(".checklist-count")).to_have_text("1/2")
        assert page.evaluate(LOW_CONTRAST_TEXT, AA) == [], email
        if email != user.email:  # what the viewer reads: done and not done entries
            expect(dialog.get_by_role("img", name="done", exact=True)).to_have_count(1)
            expect(dialog.get_by_role("img", name="not done")).to_have_count(1)
