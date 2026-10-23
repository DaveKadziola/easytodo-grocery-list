"""The board page on phones, tablets and desktops: no sideways scrolling, controls a finger can
hit, three columns on a desktop, the phone's bottom bar, a theme that follows the system's
light or dark setting, the connection status in the header, toasts that go away by themselves,
and the item modal by keyboard."""

import re

import pytest
from factories import create_board
from playwright.sync_api import Locator, Page, expect

pytestmark = pytest.mark.e2e

TOAST_MS = 4000  # how long a toast stays
UNDO_TOAST_MS = 5000  # how long one with an Undo button stays
AA = 4.5  # WCAG AA contrast for text
# What a finger must be able to hit: every control at least 44×44 px (the checkbox through the
# label around it).
TAP_TARGETS = (
    "a, button, summary, select, input:not([type=checkbox]), label:has(> input[type=checkbox])"
)
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


def category(page: Page, name: str) -> Locator:
    return page.locator("section[data-category-id]").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )


def category_cards(page: Page) -> list[Locator]:
    return page.locator("section[data-category-id]").all()


def too_small_targets(page: Page) -> list[str]:
    """Every control on the page (header, board, bottom bar) smaller than 44×44 px."""
    return page.evaluate(
        """(selector) => Array.from(document.querySelectorAll(selector))
            .filter((el) => el.getClientRects().length > 0)
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
    at_least_44px(page.locator("summary"))  # the category menus
    at_least_44px(page.get_by_role("button", name="Add", exact=True))
    assert too_small_targets(page) == []


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
    field = page.get_by_placeholder("New category")

    expect(bar).to_be_visible()
    expect(field).to_be_hidden()  # on a phone the form shows only from the bottom bar
    box = bar.bounding_box()
    assert box["y"] + box["height"] == page.viewport_size["height"]  # stuck to the bottom
    page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
    last = category_cards(page)[-1].bounding_box()
    assert last["y"] + last["height"] <= box["y"]  # the bar never hides the last category

    bar.get_by_role("button", name="New category").tap()
    expect(field).to_be_focused()
    field.fill("Frozen")
    field.press("Enter")
    expect(category(page, "Frozen")).to_be_visible()
    expect(field).to_be_focused()

    bar.get_by_text("Boards", exact=True).tap()
    expect(bar.get_by_role("link", name="Groceries")).to_have_attribute("aria-current", "page")
    expect(bar.get_by_role("link", name="All boards")).to_have_attribute("href", "/boards")
    bar.get_by_role("link", name="Weekend").tap()
    expect(page).to_have_url(re.compile(rf"/boards/{other_board_id}$"))

    page.set_viewport_size({"width": 1280, "height": 800})
    expect(page.locator("#bottom-bar")).to_be_hidden()  # a phone thing


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

    menu = category(page, "Dairy").locator("summary")
    menu.click()  # the category menu, open
    assert page.evaluate(LOW_CONTRAST_TEXT, AA) == []
    menu.click()
    page.get_by_role("button", name="Bread", exact=True).click()  # the item modal
    expect(page.get_by_role("dialog")).to_be_visible()
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


def test_item_modal_keeps_the_keyboard_inside_and_closes_on_escape(logged_in_page_with_seed):
    page = logged_in_page_with_seed
    opener = page.get_by_role("button", name="Bread", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog")
    name = dialog.get_by_label("Name")
    expect(name).to_be_focused()

    for control in (
        dialog.get_by_label("Description"),
        dialog.get_by_label("Category"),
        dialog.get_by_role("button", name="Delete item"),
        dialog.get_by_role("button", name="Cancel"),
        dialog.get_by_role("button", name="Save"),
        name,  # round again: never out to the page behind
    ):
        page.keyboard.press("Tab")
        expect(control).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Save")).to_be_focused()

    page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
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
