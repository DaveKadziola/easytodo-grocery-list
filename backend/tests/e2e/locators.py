"""Where things are on the board page, for the browser tests that share it: the category cards,
their items, adding an item, and what a finger must be able to hit."""

from playwright.sync_api import Locator, Page, expect

# What a finger must be able to hit: every control at least 44×44 px (a checkbox or a radio button
# through the label around it).
TAP_TARGETS = (
    "a, button, summary, select, input:not([type=checkbox]):not([type=radio]),"
    " label:has(> input[type=checkbox]), label:has(> input[type=radio])"
)


def category(page: Page, name: str) -> Locator:
    return page.locator("section[data-category-id]").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )


def items_of(page: Page, category_name: str) -> Locator:
    return category(page, category_name).locator("li[data-item-id]")


def item(page: Page, name: str) -> Locator:
    """The row of the item ``name``, found by the button with its name: the avatar and the
    other text in the row (initials, counts) don't get in the way. (A viewer has no such button:
    the name is plain text on the pages of those who may not edit.)"""
    return page.locator("li[data-item-id]").filter(
        has=page.get_by_role("button", name=name, exact=True)
    )


def add_item(page: Page, category_name: str, name: str) -> None:
    """Type ``name`` into the category's "Add item" field, press "Add" and wait for the item."""
    section = category(page, category_name)
    section.get_by_placeholder("Add item").fill(name)
    section.get_by_role("button", name="Add", exact=True).click()
    expect(items_of(page, category_name).filter(has_text=name)).to_have_count(1)
