"""The board page on phones, tablets and desktops: no sideways scrolling, controls a finger can
hit, three columns on a desktop, the phone's bottom bar, a theme that follows the system's
light or dark setting, the connection status in the header, toasts that go away by themselves,
and the item modal by keyboard."""

import re

import pytest
from factories import create_board, create_category, create_checklist_entry, create_item
from playwright.sync_api import Locator, Page, expect
from sqlalchemy import select

from e2e.locators import TAP_TARGETS, category
from easytodo.models import Item

pytestmark = pytest.mark.e2e

TOAST_MS = 4000  # how long a toast stays
UNDO_TOAST_MS = 5000  # how long one with an Undo button stays
AA = 4.5  # WCAG AA contrast for text
# Text the theme leaves hard to read: under 4.5:1 (WCAG AA) against what is behind it, done
# items included (greyed, still to be read). Decorations (aria-hidden) are no text to read, and
# screen-reader-only text is not seen.
LOW_CONTRAST_TEXT = r"""
(minimum) => {
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const rgba = (color) => {  // any CSS colour (Tailwind's are oklch) as sRGB 0..1 and alpha
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return a === 0 ? null : [r / 255, g / 255, b / 255, a / 255];
  };
  const background = (el) => {  // the layers behind el down to the first opaque one, blended
    const layers = [];
    for (let node = el; node instanceof Element; node = node.parentElement) {
      const color = rgba(getComputedStyle(node).backgroundColor);
      if (color) layers.push(color);
      if (color && color[3] === 1) break;
    }
    return layers.reduceRight(
      (under, [r, g, b, a]) => [r * a + under[0] * (1 - a), g * a + under[1] * (1 - a), b * a + under[2] * (1 - a)],
      [1, 1, 1],
    );
  };
  const luminance = ([r, g, b]) => {
    const linear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  };
  const contrast = (x, y) => {
    const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
    return (hi + 0.05) / (lo + 0.05);
  };
  return Array.from(document.body.querySelectorAll("*"))
    .filter((el) => Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim()))
    .filter((el) => {
      const box = el.getBoundingClientRect();
      return box.width > 1 && box.height > 1 && getComputedStyle(el).visibility === "visible";
    })
    .filter((el) => !el.closest("[aria-hidden=true]"))
    .map((el) => [el, contrast(rgba(getComputedStyle(el).color), background(el))])
    .filter(([, ratio]) => ratio < minimum)
    .map(([el, ratio]) => `${el.outerHTML.slice(0, 80)} has ${ratio.toFixed(2)}:1`);
}
"""


def category_cards(page: Page) -> list[Locator]:
    return page.locator("section[data-category-id]").all()


def too_small_targets(page: Page) -> list[str]:
    """Every control shown on the page (header, board, bottom bar) smaller than 44×44 px. Not those
    in a closed menu: checkVisibility() knows they are skipped, getClientRects() still lists them."""
    return page.evaluate(
        """(selector) => Array.from(document.querySelectorAll(selector))
            .filter((el) => el.checkVisibility())
            .map((el) => [el.outerHTML.slice(0, 80), el.getBoundingClientRect()])
            .filter(([, box]) => box.width < 44 || box.height < 44)
            .map(([html, box]) => `${html} is ${box.width}x${box.height}`)""",
        TAP_TARGETS,
    )


def at_least_44px(locator: Locator) -> None:
    assert locator.count() > 0
    for element in locator.all():
        box = element.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44, (element, box)


@pytest.mark.parametrize("width", [360, 768, 1280])
def test_no_horizontal_scroll(logged_in_page_with_seed, width):
    page = logged_in_page_with_seed
    page.set_viewport_size({"width": width, "height": 800})

    assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth")


def test_touch_targets_at_least_44px_on_phone(logged_in_page_mobile, seeded_board, open_board):
    page = logged_in_page_mobile
    open_board(page, seeded_board)

    at_least_44px(page.locator("label:has(> input[type=checkbox])"))  # where a tick lands
    at_least_44px(page.locator("summary"))  # the sort and category menus
    at_least_44px(page.get_by_role("button", name="Add", exact=True))
    assert too_small_targets(page) == []


ITEMS = ["Butter", "Cheese", "Milk", "Yogurt"]


def seed_categories(seed, user, names: list[str]) -> int:
    """A board "Groceries" of ``user`` with these categories, each holding the same four items
    (enough for several categories to be taller than a phone's screen)."""

    async def make(db) -> int:
        board = await create_board(db, user, "Groceries")
        for position, name in enumerate(names):
            category = await create_category(db, board, name, position)
            for item_name in ITEMS:
                await create_item(db, category, item_name)
        return board.id

    return seed(make)


def reachable(control: Locator) -> bool:
    """Whether a finger on the middle of ``control``, scrolled into view first, lands on it (or in
    it) and not on what covers it, such as the phone's bottom bar."""
    return control.evaluate(
        """(el) => {
            el.scrollIntoView({ block: "nearest" });
            const box = el.getBoundingClientRect();
            const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
            return hit === el || el.contains(hit);
        }"""
    )


@pytest.mark.parametrize(
    "names", [["Dairy"], ["Dairy", "Bakery", "Fruit"]], ids=["one category", "last of three"]
)
def test_sort_menu_fits_a_phone(logged_in_page_mobile, seed, user, open_board, names):
    page = logged_in_page_mobile
    open_board(page, seed_categories(seed, user, names))
    last = names[-1]  # its menu would open at the very end of the page
    card = category(page, last)
    sort_button = card.get_by_label(f"Sort {last}")
    menu = card.locator("details", has=page.get_by_label(f"Sort {last}"))
    items = card.locator("li[data-item-id]")
    if len(names) > 1:  # the last card is below the fold: this tests the page scrolled to it
        assert card.bounding_box()["y"] > page.viewport_size["height"]

    sort_button.tap()
    expect(menu.locator(".menu")).to_be_visible()

    assert too_small_targets(page) == []  # the options (through their labels) and the buttons
    box = menu.locator(".menu").bounding_box()
    width = page.viewport_size["width"]
    assert box["x"] >= 0 and box["x"] + box["width"] <= width, box
    bar = page.locator("#bottom-bar").bounding_box()
    assert 0 <= box["y"] and box["y"] + box["height"] <= bar["y"], (box, bar)  # above the bar
    assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth")
    controls = [
        *menu.locator("label").all(),
        menu.get_by_role("button", name="Apply", exact=True),
        menu.get_by_role("button", name="Use my default"),
    ]
    assert len(controls) == 9
    assert [c.inner_text() for c in controls if not reachable(c)] == []  # none under the bar

    menu.get_by_text("Z–A / newest first").tap()
    menu.get_by_role("button", name="Apply", exact=True).tap()
    expect(sort_button).to_have_class(re.compile(r"\bsort-custom\b"))
    expect(items).to_have_text([re.compile(name) for name in reversed(ITEMS)])

    sort_button.tap()  # the menu again, now with a choice of its own to take back
    assert reachable(menu.get_by_role("button", name="Use my default"))
    menu.get_by_role("button", name="Use my default").tap()
    expect(sort_button).not_to_have_class(re.compile("sort-custom"))
    expect(items).to_have_text([re.compile(name) for name in ITEMS])


def test_desktop_shows_three_columns(logged_in_page_with_seed):
    page = logged_in_page_with_seed
    page.set_viewport_size({"width": 1280, "height": 900})

    lefts = {round(card.bounding_box()["x"]) for card in category_cards(page)}
    assert len(lefts) >= 3, lefts


def test_phone_bottom_bar_adds_categories_and_switches_boards(
    logged_in_page_mobile, seed, user, seeded_board, open_board
):
    other_board_id = seed(lambda db: create_board(db, user, "Weekend")).id
    page = logged_in_page_mobile
    open_board(page, seeded_board)
    bar = page.locator("#bottom-bar")
    opener = page.get_by_role("button", name="New category")
    dialog = page.get_by_role("dialog", name="New category")
    name = dialog.get_by_label("Category name")

    expect(bar).to_be_visible()
    expect(opener).to_have_count(1)  # the bar's: the one in the board header shows from 640 px
    expect(dialog).to_be_hidden()
    box = bar.bounding_box()
    assert box["y"] + box["height"] == page.viewport_size["height"]  # stuck to the bottom
    page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
    last = category_cards(page)[-1].bounding_box()
    assert last["y"] + last["height"] <= box["y"]  # the bar never hides the last category

    bar.get_by_role("button", name="New category").tap()
    expect(dialog).to_be_visible()
    expect(name).to_be_focused()
    at_least_44px(dialog.locator("label:has(> input[type=radio])"))  # where a tap on a radio lands
    at_least_44px(dialog.get_by_role("button"))
    assert too_small_targets(page) == []
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []
    name.fill("Frozen")
    dialog.get_by_text("On top").tap()
    expect(dialog.get_by_label("On top")).to_be_checked()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    expect(page.locator("section[data-category-id] h2").first).to_have_text("Frozen")
    expect(category(page, "Frozen").get_by_placeholder("Add item")).to_be_focused()

    bar.get_by_text("Boards", exact=True).tap()
    expect(bar.get_by_role("link", name="Groceries")).to_have_attribute("aria-current", "page")
    expect(bar.get_by_role("link", name="All boards")).to_have_attribute("href", "/boards")
    bar.get_by_role("link", name="Weekend").tap()
    expect(page).to_have_url(re.compile(rf"/boards/{other_board_id}$"))

    page.set_viewport_size({"width": 1280, "height": 800})
    expect(page.locator("#bottom-bar")).to_be_hidden()  # a phone thing
    expect(opener).to_have_count(1)  # now the header's


def test_phone_shows_the_list_right_below_the_board_name(
    logged_in_page_mobile, seeded_board, open_board
):
    page = logged_in_page_mobile
    open_board(page, seeded_board)  # as the owner, with the most controls

    heading = page.get_by_role("heading", level=1).bounding_box()
    first_item = page.locator("li[data-item-id]").first.bounding_box()
    assert first_item["y"] - (heading["y"] + heading["height"]) < 160, (heading, first_item)
    assert page.locator("li[data-item-id]:visible").count() >= 3  # without scrolling:
    assert first_item["y"] + first_item["height"] < page.viewport_size["height"] / 2
    expect(page.get_by_label("Board actions")).to_have_count(0)  # board management: on the list


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_follows_the_system_and_stays_readable(
    signed_in_page, seeded_board, open_board, scheme
):
    page = signed_in_page(color_scheme=scheme)
    open_board(page, seeded_board)

    body = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page_is_dark = page.evaluate(
        """(color) => {
            const ctx = document.createElement("canvas").getContext("2d");
            ctx.fillStyle = color;
            ctx.fillRect(0, 0, 1, 1);
            const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
            return r + g + b < 3 * 128;
        }""",
        body,
    )
    assert page_is_dark is (scheme == "dark"), body

    menu = category(page, "Dairy").get_by_label("Category actions")
    menu.click()  # the category menu, open
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []
    menu.click()
    sort_menu = category(page, "Dairy").get_by_label("Sort Dairy")
    sort_menu.click()  # and the sort menu: its legends, options and buttons
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []
    sort_menu.click()
    page.get_by_role("button", name="Bread", exact=True).click()  # the item modal
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#item-details")).to_contain_text("Modified")  # with its details
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []
    for path in ("/boards", "/boards/archived", f"/boards/{seeded_board}/archive", "/account"):
        page.goto(path)
        assert page.evaluate(LOW_CONTRAST_TEXT, AA) == [], path


def test_connection_status_is_in_the_header(logged_in_page_with_seed):
    status = logged_in_page_with_seed.locator("header #conn-status")

    expect(status).to_have_text("Live")
    expect(status).to_have_attribute("data-state", "live")


def toast_lifetime(page: Page, detail: dict, lifetime_ms: int) -> tuple[str, float | None]:
    """Show the toast ``detail`` describes, as an HX-Trigger toast header does; returns its text and
    how long it stayed in ms (None: it was still there after twice ``lifetime_ms``)."""
    return page.evaluate(
        """({ detail, lifetimeMs }) => new Promise((resolve) => {
            const toasts = document.getElementById("toasts");
            let shown = "", start = 0;
            new MutationObserver((records, observer) => {
              for (const record of records) {
                if (record.addedNodes.length) [shown, start] = [toasts.textContent, performance.now()];
                if (record.removedNodes.length) {
                  observer.disconnect();
                  resolve([shown, performance.now() - start]);
                }
              }
            }).observe(toasts, { childList: true });
            setTimeout(() => resolve([shown, null]), 2 * lifetimeMs);  // never removed
            document.body.dispatchEvent(new CustomEvent("toast", { bubbles: true, detail }));
        })""",
        {"detail": detail, "lifetimeMs": lifetime_ms},
    )


def test_toasts_close_by_themselves_after_4_seconds(logged_in_page_with_seed):
    detail = {"level": "info", "message": "Saved."}

    shown, removed_after_ms = toast_lifetime(logged_in_page_with_seed, detail, TOAST_MS)

    assert shown == "Saved."
    assert removed_after_ms is not None, "the toast stayed"
    assert TOAST_MS - 50 <= removed_after_ms <= TOAST_MS + 500


def test_undo_toast_closes_after_5_seconds(logged_in_page_with_seed):
    detail = {  # as the answer to a DELETE of an item brings it
        "level": "info",
        "message": "Item deleted.",
        "undo": {"url": "/items/1/restore", "swap_into": "#category-1"},
    }

    shown, removed_after_ms = toast_lifetime(logged_in_page_with_seed, detail, UNDO_TOAST_MS)

    assert shown == "Item deleted.Undo"
    assert removed_after_ms is not None, "the toast stayed"
    assert UNDO_TOAST_MS - 50 <= removed_after_ms <= UNDO_TOAST_MS + 500


def test_item_modal_keeps_the_keyboard_inside_and_closes_on_escape(logged_in_page_with_seed, seed):
    page = logged_in_page_with_seed

    async def add_entry(db) -> None:
        await create_checklist_entry(
            db, await db.scalar(select(Item).where(Item.name == "Bread")), "Rye"
        )

    seed(add_entry)
    opener = page.get_by_role("button", name="Bread", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog")
    name = dialog.get_by_label("Name")
    expect(name).to_be_focused()
    expect(dialog.get_by_label("Edit Rye")).to_be_visible()  # the details are loaded

    for control in (
        dialog.get_by_label("Description"),
        dialog.get_by_label("Category"),
        dialog.get_by_role("button", name="Delete item"),
        dialog.get_by_role("button", name="Cancel"),
        dialog.get_by_role("button", name="Save"),
        # The checklist's controls come after the form: each entry's handle (the keyboard moves
        # the entry with the arrow keys), tick, text and delete button, then the new entry.
        dialog.get_by_role("button", name="Move Rye"),
        dialog.get_by_role("checkbox", name="Rye"),
        dialog.get_by_label("Edit Rye"),
        dialog.get_by_role("button", name="Delete Rye"),
        dialog.get_by_label("Add entry"),
        dialog.get_by_role("button", name="Add", exact=True),
        # Then the attachments' form (the file field is one stop: "Choose file" in it).
        dialog.get_by_label("Add a file"),
        dialog.get_by_role("button", name="Upload"),
        name,  # round again: never out to the page behind
    ):
        page.keyboard.press("Tab")
        expect(control).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Upload")).to_be_focused()

    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def test_item_modal_closes_on_escape_after_a_click_on_its_text(logged_in_page_with_seed):
    page = logged_in_page_with_seed
    opener = page.get_by_role("button", name="Bread", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Name")).to_be_focused()

    dialog.get_by_role("heading", name="Edit item").click()  # text: the focus stays in the dialog
    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def test_item_modal_focus_trap_skips_what_cannot_take_focus(logged_in_page_with_seed):
    page = logged_in_page_with_seed
    page.get_by_role("button", name="Bread", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("#item-details")).to_contain_text("Modified")  # loaded
    page.evaluate(
        """() => {
            const details = document.getElementById("item-details");
            const link = Object.assign(document.createElement("a"), { href: "#", textContent: "Link" });
            const disabled = Object.assign(document.createElement("button"), { disabled: true, textContent: "Off" });
            const hidden = Object.assign(document.createElement("button"), { hidden: true, textContent: "Gone" });
            details.append(link, disabled, hidden);
        }"""
    )
    link = dialog.get_by_role("link", name="Link")
    dialog.get_by_role("button", name="Save").focus()

    for control in (
        dialog.get_by_label("Add entry"),
        dialog.get_by_role("button", name="Add", exact=True),
        dialog.get_by_label("Add a file"),
        dialog.get_by_role("button", name="Upload"),
        link,
    ):
        page.keyboard.press("Tab")
        expect(control).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.get_by_label("Name")).to_be_focused()  # round again, not into the disabled one
    page.keyboard.press("Shift+Tab")
    expect(link).to_be_focused()


def test_new_category_dialog_keeps_the_keyboard_inside_and_the_radios_one_stop(
    logged_in_page_with_seed,
):
    page = logged_in_page_with_seed
    opener = page.get_by_role("button", name="New category")
    opener.click()
    dialog = page.get_by_role("dialog", name="New category")
    name = dialog.get_by_label("Category name")
    bottom, top = dialog.get_by_label("At the bottom"), dialog.get_by_label("On top")
    cancel = dialog.get_by_role("button", name="Cancel")
    add = dialog.get_by_role("button", name="Add category")
    expect(name).to_be_focused()

    page.keyboard.press("Tab")
    expect(bottom).to_be_focused()  # the group is entered once, at the checked one
    focus_ring = bottom.evaluate(
        "(el) => { const s = getComputedStyle(el); return [s.outlineStyle, s.outlineWidth]; }"
    )
    assert focus_ring[0] != "none" and focus_ring[1] != "0px", focus_ring  # the focus shows
    page.keyboard.press("ArrowDown")  # within the group, as natively: moves and checks
    expect(top).to_be_focused()
    expect(top).to_be_checked()
    expect(bottom).not_to_be_checked()
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()  # out of the group at once, not through "At the bottom"
    page.keyboard.press("Shift+Tab")
    expect(top).to_be_focused()  # and back in at the checked one
    page.keyboard.press("Shift+Tab")
    expect(name).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(add).to_be_focused()  # round the other way, never out to the page behind
    page.keyboard.press("Tab")
    expect(name).to_be_focused()

    # Text cannot take the focus, so a click on it leaves it on the dialog's panel: Escape is still
    # heard.
    dialog.get_by_role("heading", name="New category").click()
    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


@pytest.mark.parametrize("close_with", ["Escape", "Cancel"])
def test_new_category_dialog_keeps_the_keyboard_after_a_refusal(
    logged_in_page_with_seed, close_with
):
    page = logged_in_page_with_seed
    opener = page.get_by_role("button", name="New category")
    opener.click()
    dialog = page.get_by_role("dialog", name="New category")
    name = dialog.get_by_label("Category name")
    name.fill("Dairy")  # taken

    dialog.get_by_role("button", name="Add category").click()  # a click disables the button

    expect(page.locator("#toasts")).to_contain_text("A category with this name already exists.")
    expect(dialog.get_by_role("button", name="Add category")).to_be_enabled()
    expect(name).to_be_focused()  # not lost with the button: Escape is still heard
    expect(name).to_have_value("Dairy")
    expect(page.locator("#board-content")).to_have_attribute("inert", "")
    if close_with == "Escape":
        page.keyboard.press("Escape")
    else:
        dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#board-content")).not_to_have_attribute("inert", "")
    expect(opener).to_be_focused()


def test_every_icon_control_has_a_label(logged_in_page_with_seed):
    page = logged_in_page_with_seed

    # Hidden ones too (the category menus' contents, the item modal): a name made of letters.
    unnamed = page.evaluate(
        """() => Array.from(document.querySelectorAll("button, summary, a[href]"))
            .filter((el) => {
              const labelledBy = (el.getAttribute("aria-labelledby") || "").split(" ")
                .map((id) => document.getElementById(id)?.textContent || "").join(" ");
              const name = el.getAttribute("aria-label") || labelledBy || el.textContent;
              return !/[A-Za-z]/.test(name);
            })
            .map((el) => el.outerHTML.slice(0, 120))"""
    )

    assert unnamed == []
