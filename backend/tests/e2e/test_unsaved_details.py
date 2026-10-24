"""The item's sheet asks before it throws away what is in its details and was not saved: a file
that was chosen and not uploaded, text typed into "Add entry", a checklist entry whose name was
edited. Esc, "Cancel" and a tap beside the sheet ask the same question as for an edited name;
"Save" (which saves the name, the description and the category, none of those) asks its own."""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import watch_console
from e2e.locators import item
from e2e.test_attachments_ui import PHOTO
from e2e.test_board_ui import seed_board
from e2e.test_checklist_ui import add_entries, open_item

pytestmark = pytest.mark.e2e

DISCARD = "Discard your changes to this item?"
PATCHED = re.compile(r"/items/\d+$")


def choose_a_file(dialog) -> None:
    dialog.get_by_label("Add a file").set_input_files(files=[PHOTO])


def type_an_entry(dialog) -> None:
    dialog.get_by_label("Add entry").fill("Eggs")


def edit_an_entry(dialog) -> None:
    dialog.get_by_label("Edit Rye").fill("Rye bread")


def still_there(dialog) -> dict[str, object]:
    """What the details hold now, to compare before and after a refused close."""
    return {
        "files": dialog.get_by_label("Add a file").evaluate("input => input.files.length"),
        "entry": dialog.get_by_label("Add entry").input_value(),
        "rye": dialog.get_by_label(re.compile("^Edit Rye")).input_value(),
    }


UNSAVED = {
    "a chosen file": (choose_a_file, "the file you have not uploaded"),
    "text in Add entry": (type_an_entry, "the entry you have not added"),
    "an edited entry": (edit_an_entry, "your changes to a checklist entry"),
}
CLOSING: dict[str, Callable[[Page, object], None]] = {
    "Escape": lambda page, dialog: page.keyboard.press("Escape"),
    "Cancel": lambda page, dialog: dialog.get_by_role("button", name="Cancel").click(),
    "a tap beside the sheet": lambda page, dialog: page.mouse.click(x=5, y=5),
}


def answer_questions(page: Page, *answers: bool) -> list[str]:
    """Answer the next questions of the page (``window.confirm``) with these answers, in order;
    returns the list that collects what was asked."""
    asked: list[str] = []
    pending = list(answers)

    def reply(question) -> None:
        asked.append(question.message)
        question.accept() if pending.pop(0) else question.dismiss()

    page.on("dialog", reply)
    return asked


@pytest.fixture
def sheet(logged_in_page, seed, user, open_board):
    """The page, with the sheet of "Milk" (whose checklist has "Rye") open."""
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    add_entries(seed, "Milk", "Rye")
    open_board(page, board_id)
    return page, open_item(page, "Milk")


@pytest.mark.parametrize("closing", CLOSING)
@pytest.mark.parametrize("unsaved", UNSAVED)
def test_closing_asks_first_and_cancel_keeps_the_sheet_as_it_was(sheet, unsaved, closing):
    page, dialog = sheet
    problems = watch_console(page)
    UNSAVED[unsaved][0](dialog)
    before = still_there(dialog)
    asked = answer_questions(page, False, True)

    CLOSING[closing](page, dialog)

    assert asked == [DISCARD]
    expect(dialog).to_be_visible()  # "Cancel" in the question: nothing was lost
    assert still_there(dialog) == before

    CLOSING[closing](page, dialog)  # and "OK" lets go of it
    expect(dialog).to_be_hidden()
    assert asked == [DISCARD, DISCARD]
    assert problems == []


@pytest.mark.parametrize("unsaved", UNSAVED)
def test_save_asks_before_it_discards_and_sends_nothing_until_it_is_answered(sheet, unsaved):
    page, dialog = sheet
    make_unsaved, phrase = UNSAVED[unsaved]
    make_unsaved(dialog)
    dialog.get_by_label("Name").fill("Oat milk")  # saved, but only once the question is answered
    sent: list[str] = []
    page.on(
        "request", lambda request: sent.append(request.method) if request.method != "GET" else None
    )
    asked = answer_questions(page, False, True)

    dialog.get_by_role("button", name="Save").click()

    assert asked == [f"Discard {phrase}?"]
    expect(dialog).to_be_visible()
    assert sent == []  # not even the name
    expect(dialog.get_by_label("Name")).to_have_value("Oat milk")

    with page.expect_response(PATCHED):
        dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    assert asked == [f"Discard {phrase}?"] * 2
    expect(item(page, "Oat milk")).to_be_visible()


def test_save_names_everything_that_is_unsaved(sheet):
    page, dialog = sheet
    choose_a_file(dialog)
    type_an_entry(dialog)
    asked = answer_questions(page, False)

    dialog.get_by_role("button", name="Save").click()

    assert asked == ["Discard the file you have not uploaded and the entry you have not added?"]
    expect(dialog).to_be_visible()


@pytest.mark.parametrize("closing", [*CLOSING, "Save"])
def test_nothing_unsaved_closes_without_a_question(sheet, closing):
    page, dialog = sheet
    asked = answer_questions(page)  # a question would fail the test: no answer is given

    if closing == "Save":
        dialog.get_by_role("button", name="Save").click()
    else:
        CLOSING[closing](page, dialog)

    expect(dialog).to_be_hidden()
    assert asked == []


def test_an_entry_that_was_added_or_saved_is_not_unsaved(sheet):
    page, dialog = sheet
    asked = answer_questions(page)
    new_entry = dialog.get_by_label("Add entry")
    new_entry.fill("Eggs")
    new_entry.press("Enter")
    expect(dialog.get_by_label("Edit Eggs")).to_have_value("Eggs")
    rye = dialog.get_by_label("Edit Rye")
    rye.fill("Rye bread")
    rye.press("Enter")
    expect(dialog.get_by_label("Edit Rye bread")).to_have_value("Rye bread")

    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    assert asked == []
