"""What a name needs to be one: a character that shows. A name of white space, zero-width spaces,
bidirectional marks or control characters is blank (display, board, category, item and checklist
entry names alike), while format characters beside visible ones stay (a zero-width joiner is part
of an emoji sequence)."""

import pytest
from helpers import toast_message
from sqlalchemy import select

from easytodo.content.validation import clean_name
from easytodo.models import Board, Category, ChecklistEntry, Item

HX = {"HX-Request": "true"}

# Written with chr(): the characters themselves are invisible (and a formatter turns escapes
# into them).
ZWSP, ZWNJ, ZWJ, BOM = chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF)
RLO, LRE, PDF = chr(0x202E), chr(0x202A), chr(0x202C)
LRI, PDI, LRM, RLM = chr(0x2066), chr(0x2069), chr(0x200E), chr(0x200F)
SOFT_HYPHEN, WORD_JOINER = chr(0x00AD), chr(0x2060)
VS16, BRAILLE_BLANK = chr(0xFE0F), chr(0x2800)
FAMILY = chr(0x1F468) + ZWJ + chr(0x1F469) + ZWJ + chr(0x1F467)
RAINBOW_FLAG = chr(0x1F3F3) + VS16 + ZWJ + chr(0x1F308)

# Format (Cf) and control (Cc) characters, alone or with white space.
INVISIBLE = [
    ZWSP,
    ZWJ,
    ZWSP + ZWNJ + ZWJ + BOM,
    RLO,
    LRE + " " + PDF,  # embeddings around a space
    LRI + PDI,  # isolates
    LRM + RLM,  # direction marks
    SOFT_HYPHEN,
    WORD_JOINER,
    " " + ZWSP + " \t" + WORD_JOINER + "\n",  # with white space
    "\x07\x1b",  # control characters
]
# Names with something visible: as typed, apart from the white space around and inside.
VISIBLE = [
    "Milk",
    FAMILY + " Rodzina",
    RAINBOW_FLAG,
    ZWSP + "Milk",  # a zero-width space before text is kept, not stripped
    "Milk" + ZWSP,
    "a" + RLO + "b",
    "e" + chr(0x0301),  # a letter and a combining mark
    "-",
    "0",
    BRAILLE_BLANK,  # a symbol, not white space or a format character
]


@pytest.mark.parametrize("name", INVISIBLE)
def test_a_name_without_a_visible_character_is_blank(name):
    from easytodo.errors import ValidationFailed

    with pytest.raises(ValidationFailed) as error:
        clean_name(name, max_len=80, label="Board name")

    assert error.value.message == "Board name must be 1–80 characters."


@pytest.mark.parametrize("name", VISIBLE)
def test_a_name_with_a_visible_character_keeps_all_its_characters(name):
    assert clean_name(f"  {name} ", max_len=80, label="Board name") == name


def test_the_limit_and_the_empty_name_are_as_before():
    from easytodo.errors import ValidationFailed

    for name, max_len in (("", 5), ("   ", 5), ("abcdef", 5)):
        with pytest.raises(ValidationFailed):
            clean_name(name, max_len=max_len, label="Name")
    assert clean_name("abcde", max_len=5, label="Name") == "abcde"


ROUTES = [
    ("/boards", "name", "Board name must be 1–80 characters.", Board.name),
    (
        "/boards/{board}/categories",
        "name",
        "Category name must be 1–120 characters.",
        Category.name,
    ),
    ("/categories/{category}/items", "name", "Item name must be 1–200 characters.", Item.name),
    (
        "/items/{item}/checklist",
        "text",
        "Checklist entry must be 1–200 characters.",
        ChecklistEntry.text,
    ),
]


@pytest.mark.parametrize(
    "name", [ZWSP + ZWJ, RLO, " " + WORD_JOINER + " "], ids=["zwsp", "rlo", "wj"]
)
@pytest.mark.parametrize(
    ("path", "field", "message", "column"), ROUTES, ids=["board", "category", "item", "entry"]
)
async def test_a_blank_name_is_refused_by_every_route_that_takes_one(
    editor_client, db, board, category, item, path, field, message, column, name
):
    url = path.format(board=board.id, category=category.id, item=item.id)

    # The board form is a page of its own (the message is in the page); the rest are htmx forms.
    r = await editor_client.post(url, data={field: name}, headers={} if path == "/boards" else HX)

    assert r.status_code == 422
    assert message in (r.text if path == "/boards" else toast_message(r))
    assert await db.scalar(select(column).where(column == name)) is None  # nothing was made


@pytest.mark.parametrize("name", VISIBLE[:3], ids=["milk", "family", "flag"])
@pytest.mark.parametrize(
    ("path", "field", "message", "column"), ROUTES, ids=["board", "category", "item", "entry"]
)
async def test_a_name_with_a_joiner_and_something_visible_is_accepted(
    editor_client, db, board, category, item, path, field, message, column, name
):
    url = path.format(board=board.id, category=category.id, item=item.id)

    r = await editor_client.post(url, data={field: name}, headers=HX)

    assert r.status_code in (200, 201, 303)
    # Saved as typed: a joiner is not stripped from a name that has something visible.
    assert await db.scalar(select(column).where(column == name)) == name
