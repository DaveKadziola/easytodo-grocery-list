"""Attachments in the item's modal in Chromium: a file added and shown as a thumbnail, its count on
the tile (here when the modal closes, on the other device live), its page, deleting it, and the
section on a phone: the controls a finger can hit, nothing wider than the screen, the file picker
under the page's Permissions-Policy.

What a headless Chromium cannot show (a PDF in the browser's viewer, the phone's camera) is the
manual check in the deployment checklist; here the PDF's response is checked."""

import re

import pytest
from attachments import samples
from playwright.sync_api import Page, expect

from e2e.conftest import watch_console
from e2e.locators import item
from e2e.test_board_ui import seed_board
from e2e.test_realtime import LIVE_UPDATE_MS, SETTLE_MS
from e2e.test_responsive import at_least_44px, too_small_targets
from easytodo.attachments.routes import PDF_CSP

pytestmark = pytest.mark.e2e

UPLOADED = re.compile(r"/items/\d+/attachments$")
# No hyphen, space or other place to break at: it must wrap anywhere (wrap-anywhere).
LONG_NAME = "Quarterly_report_with_a_name_that_has_no_place_to_break_2026_final_v2.txt"


def file_of(name: str, mime_type: str, content: bytes) -> dict:
    """A file for ``set_input_files``, from memory."""
    return {"name": name, "mimeType": mime_type, "buffer": content}


PHOTO = file_of("photo.png", "image/png", samples.png((600, 400)))
NOTE = file_of("note.txt", "text/plain", samples.TEXT)
RECEIPT = file_of("receipt.pdf", "application/pdf", samples.PDF)


def open_item(page: Page, name: str, *, tap: bool = False):
    """Open the modal of the item ``name`` and wait for its attachments; returns the modal."""
    button = item(page, name).get_by_role("button", name=name, exact=True)
    button.tap() if tap else button.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("#item-attachments")).to_be_visible()
    return dialog


def upload(page: Page, dialog, file: dict, *, tap: bool = False) -> None:
    """Choose ``file`` in "Add a file", press "Upload" and wait for the details to come back
    with it."""
    count = dialog.locator("li[data-attachment-id]").count()
    dialog.get_by_label("Add a file").set_input_files(files=[file])
    with page.expect_response(UPLOADED) as response:
        upload_button = dialog.get_by_role("button", name="Upload", exact=True)
        upload_button.tap() if tap else upload_button.click()
    assert response.value.status == 200
    expect(dialog.locator("li[data-attachment-id]")).to_have_count(count + 1)
    expect(dialog.get_by_label("Add a file")).to_be_focused()  # ready for the next one


def tile_count(page: Page, name: str):
    """The attachment count in the tile of the item ``name``."""
    return item(page, name).locator(".attachment-count")


def test_attach_view_and_delete_an_image(two_logged_in_pages):
    a, b = two_logged_in_pages
    problems = watch_console(a)
    problems_b = watch_console(b)
    questions: list[str] = []
    a.on("dialog", lambda question: (questions.append(question.message), question.accept()))
    dialog = open_item(a, "Milk")
    expect(dialog.get_by_text("No attachments yet.")).to_be_visible()

    # A file that is chosen and not sent is lost when the details are replaced (a checklist
    # entry is added): the browser lets no page set a file field. It is chosen again.
    file_field = dialog.get_by_label("Add a file")
    file_field.set_input_files(files=[PHOTO])
    expect(file_field).to_have_value(re.compile(r"photo\.png$"))
    new_entry = dialog.get_by_label("Add entry")
    new_entry.fill("Eggs")
    new_entry.press("Enter")
    expect(dialog.get_by_label("Edit Eggs")).to_have_value("Eggs")
    assert dialog.get_by_label("Add a file").evaluate("input => input.files.length") == 0

    upload(a, dialog, PHOTO)

    thumbnail = dialog.locator("li[data-attachment-id] img")
    expect(thumbnail).to_have_js_property("naturalWidth", 480)  # the 480 px thumbnail, loaded
    expect(dialog.get_by_text("No attachments yet.")).to_have_count(0)
    expect(tile_count(b, "Milk")).to_have_text("📎 1", timeout=LIVE_UPDATE_MS)
    a.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(tile_count(a, "Milk")).to_have_text("📎 1")  # this device follows the closed modal

    # The file's page shows the picture itself.
    dialog = open_item(a, "Milk")
    dialog.get_by_role("link", name="photo.png").click()
    picture = a.get_by_role("img", name="photo.png")
    expect(picture).to_have_js_property("naturalWidth", 600)  # the original, not the thumbnail
    a.get_by_role("link", name="Back to the board").click()
    expect(a.locator("#conn-status")).to_have_text("Live")

    dialog = open_item(a, "Milk")
    dialog.get_by_role("button", name="Delete photo.png").click()
    expect(dialog.get_by_text("No attachments yet.")).to_be_visible()
    expect(dialog.get_by_label("Add a file")).to_be_focused()
    assert questions == ["Delete “photo.png”?"]
    expect(tile_count(b, "Milk")).to_have_count(0, timeout=LIVE_UPDATE_MS)
    assert problems == [] and problems_b == []


def test_a_chosen_file_is_not_swapped_away_by_a_live_update(two_logged_in_pages):
    a, b = two_logged_in_pages
    problems = watch_console(a)
    dialog_a, dialog_b = open_item(a, "Milk"), open_item(b, "Milk")
    dialog_a.get_by_label("Add a file").set_input_files(files=[PHOTO])

    new_entry = dialog_b.get_by_label("Add entry")  # another device changes the same item
    new_entry.fill("Eggs")
    new_entry.press("Enter")
    expect(dialog_b.get_by_label("Edit Eggs")).to_have_value("Eggs")
    a.wait_for_timeout(SETTLE_MS)  # long enough for the update to show, were it not held back

    expect(dialog_a.get_by_label("Edit Eggs")).to_have_count(0)
    expect(dialog_a.get_by_label("Add a file")).to_have_value(re.compile(r"photo\.png$"))
    upload_button = dialog_a.get_by_role("button", name="Upload", exact=True)
    with a.expect_response(UPLOADED):
        upload_button.click()  # the file is sent; the answer brings the entry along
    expect(dialog_a.locator("li[data-attachment-id]")).to_have_count(1)
    expect(dialog_a.get_by_label("Edit Eggs")).to_have_value("Eggs")
    assert problems == []


def test_an_update_held_back_by_a_chosen_file_comes_once_the_file_is_cleared(two_logged_in_pages):
    a, b = two_logged_in_pages
    dialog_a, dialog_b = open_item(a, "Milk"), open_item(b, "Milk")
    file_field = dialog_a.get_by_label("Add a file")
    file_field.set_input_files(files=[PHOTO])
    new_entry = dialog_b.get_by_label("Add entry")
    new_entry.fill("Eggs")
    new_entry.press("Enter")
    a.wait_for_timeout(SETTLE_MS)
    expect(dialog_a.get_by_label("Edit Eggs")).to_have_count(0)

    file_field.set_input_files(files=[])  # the user changed their mind

    expect(dialog_a.get_by_label("Edit Eggs")).to_have_value("Eggs", timeout=LIVE_UPDATE_MS * 2)


def test_a_file_over_the_limit_is_refused_by_the_real_server_and_the_page_goes_on(
    logged_in_page, seed, user, open_board
):
    page = logged_in_page
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)
    dialog = open_item(page, "Milk")
    big = file_of("big.txt", "text/plain", b"x" * (11 * 1024 * 1024))

    dialog.get_by_label("Add a file").set_input_files(files=[big])
    with page.expect_response(UPLOADED) as response:
        dialog.get_by_role("button", name="Upload", exact=True).click()

    assert response.value.status == 413
    expect(page.locator("#toasts")).to_have_text("Files can be at most 10 MB.")
    expect(dialog.get_by_text("No attachments yet.")).to_be_visible()
    # The connection and the page are fine: a file that fits goes through.
    upload(page, dialog, NOTE)
    expect(dialog.locator("li[data-attachment-id]")).to_have_count(1)


def test_attachment_section_fits_a_phone(logged_in_page_mobile, seed, user, open_board):
    page = logged_in_page_mobile
    problems = watch_console(page)
    board_id = seed_board(seed, user, {"Dairy": ["Milk"]})
    open_board(page, board_id)

    dialog = open_item(page, "Milk", tap=True)
    with page.expect_file_chooser() as chooser:  # the picker opens under the page's policy
        dialog.get_by_label("Add a file").tap()
    chooser.value.set_files(files=[PHOTO])
    with page.expect_response(UPLOADED):
        dialog.get_by_role("button", name="Upload", exact=True).tap()
    expect(dialog.locator("li[data-attachment-id]")).to_have_count(1)
    upload(page, dialog, NOTE, tap=True)
    upload(page, dialog, RECEIPT, tap=True)
    upload(page, dialog, file_of(LONG_NAME, "text/plain", samples.TEXT), tap=True)

    files = dialog.locator("li[data-attachment-id]")
    expect(files).to_have_count(4)
    deletes = files.get_by_role("button", name=re.compile("^Delete "))
    expect(deletes).to_have_count(4)
    assert too_small_targets(page) == []  # the whole page, the modal's controls too
    at_least_44px(deletes)
    at_least_44px(dialog.locator("li[data-attachment-id] a"))
    # A link never reaches under its Delete button, however long (and unbreakable) its name is.
    for row in files.all():
        link, delete = row.locator("a").bounding_box(), row.get_by_role("button").bounding_box()
        assert link is not None and delete is not None
        assert link["x"] + link["width"] <= delete["x"], row.inner_text()
    at_least_44px(dialog.get_by_label("Add a file"))
    at_least_44px(dialog.get_by_role("button", name="Upload", exact=True))
    assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth")
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")  # the long name wraps
    expect(dialog.get_by_text(LONG_NAME)).to_be_visible()

    # The PDF is served to the browser's viewer under its own policy; the page under the app's
    # (camera=() applies to the page's scripts, not to the file picker, which opened above).
    pdf_link = dialog.get_by_role("link", name="receipt.pdf")
    expect(pdf_link).to_have_attribute("target", "_blank")
    pdf = page.request.get(pdf_link.get_attribute("href"))
    assert pdf.status == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-security-policy"] == PDF_CSP
    assert "frame-ancestors 'none'" in pdf.headers["content-security-policy"]
    board_page = page.request.get(f"/boards/{board_id}")
    assert board_page.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert dialog.get_by_label("Add a file").get_attribute("capture") is None

    page.keyboard.press("Escape")
    expect(tile_count(page, "Milk")).to_have_text("📎 4")
    assert problems == []
