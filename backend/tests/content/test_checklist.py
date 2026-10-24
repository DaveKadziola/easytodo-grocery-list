"""The checklist inside an item: entries added, renamed, ticked, deleted and reordered from the
item's details; their limits; the lookups that leave out an item that is gone; the read-only
checklist of a viewer; the escaping of entry texts; and the count on the item's tile."""

import asyncio
import json
import re
from datetime import UTC, datetime

import pytest
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, text_of, wait_until_blocked_by
from sqlalchemy import select, text, update

from content.test_board_markup import EDIT_CONTROLS, section
from content.test_items import changed
from easytodo.content.checklist import (
    CHECKLIST_OUT_OF_DATE,
    ENTRY_GONE,
    ENTRY_MAX_LENGTH,
    MAX_ENTRIES,
    TOO_MANY_ENTRIES,
)
from easytodo.content.service import ITEM_GONE
from easytodo.models import Board, Category, ChecklistEntry, Item

HX = {"HX-Request": "true"}
ENTRY_LENGTH = "Checklist entry must be 1–200 characters."
# The count in the container of an item's tile, as its first child.
FIRST_IN_THE_TILE = re.compile(
    r'<div class="shrink-0 py-2\.5 pl-2 \*:align-top">\s*<span class="checklist-count '
)


def toast(response) -> str:
    return json.loads(response.headers["hx-trigger"])["toast"]["message"]


def resync_with(message: str) -> dict:
    return {"toast": {"level": "error", "message": message}, "resync": True}


async def entries(db, item) -> list[tuple[str, int]]:
    """The item's entries as ``(text, position)``, in their order (position, then id)."""
    rows = await db.execute(
        select(ChecklistEntry.text, ChecklistEntry.position)
        .where(ChecklistEntry.item_id == item.id)
        .order_by(ChecklistEntry.position, ChecklistEntry.id)
    )
    return [tuple(row) for row in rows]


async def item_columns(db, item) -> tuple:
    """What a checklist must not touch: the item's state and its "Modified" date."""
    row = await db.execute(
        select(Item.is_done, Item.done_at, Item.updated_at, Item.name, Item.category_id).where(
            Item.id == item.id
        )
    )
    return tuple(row.one())


async def test_add_entry_goes_last_and_answers_with_the_details(
    editor_client, board, category, item, db, published
):
    event = changed(board.id, category.id, items=(item.id,))

    r = await editor_client.post(
        f"/items/{item.id}/checklist",
        data={"text": "  Eggs  "},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{item.id}"' in r.text
    assert 'value="Eggs"' in r.text
    assert await entries(db, item) == [("Eggs", 0)]

    r = await editor_client.post(f"/items/{item.id}/checklist", data={"text": "Flour"}, headers=HX)

    assert r.status_code == 200
    assert 'value="Eggs"' in r.text and 'value="Flour"' in r.text
    assert await entries(db, item) == [("Eggs", 0), ("Flour", 1)]
    assert published == [((event,), "tab-1"), ((event,), None)]  # one event per write


async def test_rename_check_and_delete_an_entry(
    editor_client, board, category, item, db, published
):
    entry = await create_checklist_entry(db, item, "Eggs")
    event = changed(board.id, category.id, items=(item.id,))

    r = await editor_client.patch(
        f"/checklist/{entry.id}",
        data={"text": " Free  range eggs "},
        headers=HX | {"X-Client-Id": "a"},
    )

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{item.id}"' in r.text
    assert 'value="Free range eggs"' in r.text
    assert await entries(db, item) == [("Free range eggs", 0)]
    assert published == [((event,), "a")]

    async def is_done() -> bool:
        return await db.scalar(select(ChecklistEntry.is_done).where(ChecklistEntry.id == entry.id))

    published.clear()
    for expected in (True, False):  # without "done" the entry flips
        r = await editor_client.post(f"/checklist/{entry.id}/check", headers=HX)
        assert r.status_code == 200
        assert await is_done() is expected
    assert published == [((event,), None)] * 2

    published.clear()
    for _ in range(2):  # the state the user wants: the second one finds it as it is
        r = await editor_client.post(
            f"/checklist/{entry.id}/check", data={"done": "true"}, headers=HX
        )
        assert r.status_code == 200
        assert await is_done() is True
    assert [events for events, _ in published if events] == [(event,)]  # the second: nothing new
    assert " checked>" in r.text

    r = await editor_client.post(f"/checklist/{entry.id}/check", data={"done": "yes"}, headers=HX)
    assert r.status_code == 422
    assert await is_done() is True

    published.clear()
    r = await editor_client.delete(f"/checklist/{entry.id}", headers=HX)

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{item.id}"' in r.text
    assert "Free range eggs" not in r.text
    assert await entries(db, item) == []
    assert published == [((event,), None)]


async def test_delete_leaves_the_order_of_the_others(editor_client, item, db):
    for position, name in enumerate(("Eggs", "Flour", "Sugar")):
        await create_checklist_entry(db, item, name, position=position)
    flour = await db.scalar(select(ChecklistEntry.id).where(ChecklistEntry.text == "Flour"))
    await editor_client.delete(f"/checklist/{flour}", headers=HX)

    r = await editor_client.post(f"/items/{item.id}/checklist", data={"text": "Salt"}, headers=HX)

    assert r.status_code == 200
    assert await entries(db, item) == [("Eggs", 0), ("Sugar", 2), ("Salt", 3)]  # after the last


async def test_checking_every_entry_does_not_tick_the_item(editor_client, item, db):  # P10
    first = await create_checklist_entry(db, item, "Eggs", position=0)
    second = await create_checklist_entry(db, item, "Flour", position=1)
    before = await item_columns(db, item)

    for entry in (first, second):
        r = await editor_client.post(f"/checklist/{entry.id}/check", headers=HX)
        assert r.status_code == 200

    assert await item_columns(db, item) == before  # is_done, done_at and "Modified" as they were
    assert before[0] is False and before[1] is None
    assert ">2/2</span>" in r.text and "text-emerald-700" in r.text


async def test_checklist_writes_leave_the_modified_date_alone(editor_client, item, db):
    long_ago = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
    await db.execute(update(Item).where(Item.id == item.id).values(updated_at=long_ago))
    await db.commit()
    entry = await create_checklist_entry(db, item, "Eggs", position=0)

    for method, path, data in (
        ("POST", f"/items/{item.id}/checklist", {"text": "Flour"}),
        ("PATCH", f"/checklist/{entry.id}", {"text": "Free range eggs"}),
        ("POST", f"/checklist/{entry.id}/check", None),
        ("DELETE", f"/checklist/{entry.id}", None),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 200, (method, path)

    assert (await item_columns(db, item))[2] == long_ago
    assert 'Modified <time datetime="2030-01-02T03:04:00+00:00"' in r.text


async def test_reorder_needs_an_exact_permutation(
    editor_client, board, category, item, db, content_snapshot, published
):
    a, b, c = [
        await create_checklist_entry(db, item, name, position=position)
        for position, name in enumerate(("Eggs", "Flour", "Sugar"))
    ]
    stray = await create_checklist_entry(db, await create_item(db, category, "Bread"), "Yeast")
    elsewhere_category = await create_category(
        db, await create_board(db, await create_user(db, "other@example.com")), "Tools"
    )
    foreign = await create_checklist_entry(
        db, await create_item(db, elsewhere_category, "Hammer"), "Nails"
    )
    path = f"/items/{item.id}/checklist/order"

    r = await editor_client.put(
        path, data={"ids": [str(c.id), str(a.id), str(b.id)]}, headers=HX | {"X-Client-Id": "a"}
    )

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{item.id}"' in r.text
    assert await entries(db, item) == [("Sugar", 0), ("Eggs", 1), ("Flour", 2)]
    assert [r.text.index(f'value="{name}"') for name in ("Sugar", "Eggs", "Flour")] == sorted(
        r.text.index(f'value="{name}"') for name in ("Sugar", "Eggs", "Flour")
    )
    assert published == [((changed(board.id, category.id, items=(item.id,)),), "a")]

    published.clear()
    before = await content_snapshot()
    for ids in (
        [a, b],  # one missing
        [a, a, b, c],  # one twice
        [],  # none
        [a, b, c, stray],  # another item's entry
        [a, b, stray],  # instead of one of its own
        [a, b, c, foreign],  # another board's
    ):
        r = await editor_client.put(path, data={"ids": [str(e.id) for e in ids]}, headers=HX)
        assert r.status_code == 422, ids
        assert toast(r) == CHECKLIST_OUT_OF_DATE
    r = await editor_client.put(path, data={"ids": ["0", "-1", str(2**63)]}, headers=HX)
    assert r.status_code == 422

    assert await content_snapshot() == before
    assert published == []


async def test_entry_text_and_count_limits(editor_client, item, db):
    entry = await create_checklist_entry(db, item, "Eggs")
    for bad in ("", "   ", "x" * (ENTRY_MAX_LENGTH + 1)):
        for method, path in (
            ("POST", f"/items/{item.id}/checklist"),
            ("PATCH", f"/checklist/{entry.id}"),
        ):
            r = await editor_client.request(method, path, data={"text": bad}, headers=HX)
            assert r.status_code == 422, (method, len(bad))
            assert toast(r) == ENTRY_LENGTH
    r = await editor_client.post(f"/items/{item.id}/checklist", headers=HX)  # no field at all
    assert r.status_code == 422 and toast(r) == ENTRY_LENGTH
    assert await entries(db, item) == [("Eggs", 0)]

    longest = "x" * ENTRY_MAX_LENGTH
    r = await editor_client.post(f"/items/{item.id}/checklist", data={"text": longest}, headers=HX)
    assert r.status_code == 200
    r = await editor_client.patch(f"/checklist/{entry.id}", data={"text": longest}, headers=HX)
    assert r.status_code == 200
    r = await editor_client.patch(f"/checklist/{entry.id}", data={"text": "Eg\x00gs"}, headers=HX)
    assert r.status_code == 200
    assert await entries(db, item) == [("Eggs", 0), (longest, 1)]


async def test_a_checklist_holds_at_most_100_entries(editor_client, item, db):
    db.add_all(
        ChecklistEntry(item_id=item.id, text=f"Step {n}", is_done=False, position=n)
        for n in range(MAX_ENTRIES - 1)
    )
    await db.commit()

    r = await editor_client.post(f"/items/{item.id}/checklist", data={"text": "Last"}, headers=HX)

    assert r.status_code == 200
    r = await editor_client.post(
        f"/items/{item.id}/checklist", data={"text": "One more"}, headers=HX
    )

    assert r.status_code == 409
    assert toast(r) == TOO_MANY_ENTRIES
    rows = await entries(db, item)
    assert len(rows) == MAX_ENTRIES and rows[-1] == ("Last", MAX_ENTRIES - 1)

    # Another item has its own 100, and a deleted entry makes room again.
    other = await create_item(db, await db.get_one(Category, item.category_id), "Bread")
    r = await editor_client.post(f"/items/{other.id}/checklist", data={"text": "Yeast"}, headers=HX)
    assert r.status_code == 200
    first = await db.scalar(select(ChecklistEntry.id).where(ChecklistEntry.item_id == item.id))
    assert (await editor_client.delete(f"/checklist/{first}", headers=HX)).status_code == 200
    r = await editor_client.post(
        f"/items/{item.id}/checklist", data={"text": "One more"}, headers=HX
    )
    assert r.status_code == 200


@pytest.mark.parametrize("gone", ["deleted item", "archived category", "archived board"])
async def test_entries_of_a_gone_item_category_or_board_are_404(
    editor_client, board, item, db, content_snapshot, gone
):  # Review Focus 1
    entry = await create_checklist_entry(db, item, "Eggs")
    if gone == "deleted item":
        r = await editor_client.delete(f"/items/{item.id}", headers=HX)
        assert r.status_code == 200
    elif gone == "archived category":
        r = await editor_client.post(f"/categories/{item.category_id}/archive", headers=HX)
        assert r.status_code == 200
    else:
        await db.execute(
            update(Board).where(Board.id == board.id).values(archived_at=datetime.now(UTC))
        )
        await db.commit()
    before = await content_snapshot()

    for method, path, data, message in (
        ("POST", f"/items/{item.id}/checklist", {"text": "Flour"}, ITEM_GONE),
        ("PUT", f"/items/{item.id}/checklist/order", {"ids": [str(entry.id)]}, ITEM_GONE),
        ("PATCH", f"/checklist/{entry.id}", {"text": "Flour"}, ENTRY_GONE),
        ("POST", f"/checklist/{entry.id}/check", None, ENTRY_GONE),
        ("DELETE", f"/checklist/{entry.id}", None, ENTRY_GONE),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == resync_with(message), (method, path)

    assert await content_snapshot() == before


async def test_viewer_sees_a_read_only_checklist(viewer_client, item, db):
    await create_checklist_entry(db, item, "Eggs", is_done=True, position=0)
    await create_checklist_entry(db, item, "Flour", position=1)

    r = await viewer_client.get(f"/items/{item.id}/details", headers=HX)

    assert r.status_code == 200
    assert 'id="item-checklist-title">Checklist</h3>' in r.text
    assert ">1/2</span>" in r.text
    assert r.text.count('role="img" aria-label="done">✓</span>') == 1
    assert r.text.count('role="img" aria-label="not done">○</span>') == 1
    assert text_of(r.text).index("Eggs") < text_of(r.text).index("Flour")
    for control in EDIT_CONTROLS:
        assert control not in r.text, control
    assert "/checklist" not in r.text and "Add entry" not in r.text
    assert "<form" not in r.text and "<input" not in r.text

    await db.execute(ChecklistEntry.__table__.delete())
    await db.commit()
    r = await viewer_client.get(f"/items/{item.id}/details", headers=HX)

    assert r.status_code == 200
    assert "item-checklist-title" not in r.text  # nothing to show: no heading either


async def test_editor_with_no_entries_gets_the_heading_and_the_add_form(editor_client, item):
    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert '<section id="item-checklist"' in r.text
    assert 'id="item-checklist-title">Checklist</h3>' in r.text
    assert "checklist-count" not in r.text
    assert re.search(
        rf'<form hx-post="/items/{item.id}/checklist" data-refocus="checklist-new-{item.id}"'
        r' hx-disabled-elt="find button"',
        r.text,
    )
    assert re.search(
        rf'<input id="checklist-new-{item.id}" name="text" placeholder="Add entry"', r.text
    )
    assert f'<label for="checklist-new-{item.id}" class="sr-only">Add entry</label>' in r.text


async def test_editor_controls_with_escaped_text(
    editor_client, viewer_client, item, db
):  # Review Focus 3
    entry = await create_checklist_entry(db, item, NASTY, is_done=True)

    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert f'hx-post="/checklist/{entry.id}/check"' in r.text and "/toggle" not in r.text
    assert re.search(
        rf'<input type="checkbox" id="checklist-{entry.id}-done"[^>]* checked>', r.text
    )
    assert f'hx-patch="/checklist/{entry.id}"' in r.text
    assert f'hx-delete="/checklist/{entry.id}"' in r.text
    assert f'data-refocus="checklist-new-{item.id}"' in r.text
    assert re.search(r"<ul [^>]*data-sortable-checklist>", r.text)
    assert f'<li id="checklist-entry-{entry.id}" data-entry-id="{entry.id}"' in r.text
    assert f'value="{NASTY_ESCAPED}"' in r.text
    for label in (
        f"{NASTY_ESCAPED}",
        f"Edit {NASTY_ESCAPED}",
        f"Delete {NASTY_ESCAPED}",
        f"Move {NASTY_ESCAPED}",
    ):
        assert f'aria-label="{label}"' in r.text, label
    # The handle takes the focus (the keyboard's way to reorder, board.js) and is also what a drag
    # starts from; it is a span, as Firefox starts no drag from a button in the draggable row.
    handle = re.search(rf'<span id="checklist-{entry.id}-handle"[^>]*>', r.text)
    assert handle, "no handle"
    assert 'class="checklist-handle ' in handle.group(0)
    assert 'role="button"' in handle.group(0) and 'tabindex="0"' in handle.group(0)
    assert f'aria-label="Move {NASTY_ESCAPED}"' in handle.group(0)
    assert "<script>alert(1)</script>" not in r.text and "<b>" not in r.text
    # Nothing the board's scripts would take for an item or a category on the page.
    assert "data-item-id" not in r.text and "data-category-" not in r.text
    assert not re.search(r'id="(item|category)-\d', r.text)

    r = await viewer_client.get(f"/items/{item.id}/details", headers=HX)

    assert NASTY_ESCAPED in r.text
    assert "checklist-handle" not in r.text  # nothing to move for a viewer
    assert "<script>alert(1)</script>" not in r.text and "<b>" not in r.text
    assert "data-item-id" not in r.text and "data-category-" not in r.text
    assert not re.search(r'id="(item|category)-\d', r.text)


async def test_tile_shows_the_checklist_count(
    editor_client, viewer_client, board, category, item, db
):
    await db.execute(update(Item).where(Item.id == item.id).values(created_by=board.owner_id))
    await db.commit()
    paths = (f"/boards/{board.id}", f"/categories/{category.id}", f"/boards/{board.id}/content")

    for client in (editor_client, viewer_client):  # no entries: no count
        for path in paths:
            assert "checklist-count" not in (await client.get(path, headers=HX)).text

    first = await create_checklist_entry(db, item, "Eggs", position=0)
    second = await create_checklist_entry(db, item, "Flour", position=1)

    for client in (editor_client, viewer_client):
        page = (await client.get(f"/boards/{board.id}")).text
        fragment = (await client.get(f"/categories/{category.id}", headers=HX)).text
        assert section(page, category.id) == section(fragment, category.id)
        assert (
            '<span class="checklist-count text-slate-600 dark:text-slate-400"'
            ' title="0 of 2 checklist entries done">0/2</span>'
        ) in fragment
        assert FIRST_IN_THE_TILE.search(fragment)  # before the author's avatar, in the same box
        assert fragment.index("checklist-count") < fragment.index('class="avatar')

    for entry in (first, second):
        await editor_client.post(f"/checklist/{entry.id}/check", headers=HX)

    for client in (editor_client, viewer_client):
        fragment = (await client.get(f"/categories/{category.id}", headers=HX)).text
        assert (
            '<span class="checklist-count text-emerald-700 dark:text-emerald-400"'
            ' title="2 of 2 checklist entries done">2/2</span>'
        ) in fragment


async def test_tile_shows_the_count_of_an_item_nobody_added(editor_client, category, item, db):
    await create_checklist_entry(db, item, "Eggs", is_done=True)

    html = (await editor_client.get(f"/categories/{category.id}", headers=HX)).text

    assert FIRST_IN_THE_TILE.search(html)  # the box is there for the count alone
    assert ">1/1</span>" in html and 'class="avatar' not in html


@pytest.mark.parametrize(
    "held_row,method,path,data",
    [
        # A foreign key check (an entry or an attachment being added) holds FOR KEY SHARE on the
        # item; a change that keys nothing does not wait for it.
        ("items", "POST", "/items/{item}/checklist", {"text": "Flour"}),
        ("items", "PUT", "/items/{item}/checklist/order", {"ids": ["{entry}"]}),
        ("checklist_entries", "PATCH", "/checklist/{entry}", {"text": "Free range eggs"}),
        ("checklist_entries", "POST", "/checklist/{entry}/check", None),
    ],
)
async def test_checklist_writes_take_only_the_row_lock_they_need(
    editor_client, item, db, held_row, method, path, data
):
    entry = await create_checklist_entry(db, item, "Eggs")
    ids = {"item": item.id, "entry": entry.id}
    if data is not None:
        data = {
            key: [v.format(**ids) for v in value]
            if isinstance(value, list)
            else value.format(**ids)
            for key, value in data.items()
        }
    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text(f"SELECT 1 FROM {held_row} WHERE id = :id FOR KEY SHARE"),
            {"id": ids["item" if held_row == "items" else "entry"]},
        )
        request = editor_client.request(method, path.format(**ids), data=data, headers=HX)
        r = await asyncio.wait_for(request, 2)

    assert r.status_code == 200, r.headers.get("hx-trigger")


async def test_a_flip_waits_for_the_change_to_the_same_entry_it_flips(editor_client, item, db):
    """Two devices tick one entry at the same moment: the second one sees what the first made."""
    entry = await create_checklist_entry(db, item, "Eggs")

    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            update(ChecklistEntry).where(ChecklistEntry.id == entry.id).values(is_done=True)
        )
        flip = asyncio.create_task(editor_client.post(f"/checklist/{entry.id}/check", headers=HX))
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        r = await flip

    assert r.status_code == 200
    # It flipped the committed "done", not the "open" it started from.
    assert (
        await db.scalar(select(ChecklistEntry.is_done).where(ChecklistEntry.id == entry.id))
        is False
    )


async def test_entries_added_at_the_same_moment_take_one_position_each(editor_client, item, db):
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(  # the item, held as an adding request holds it
            text("SELECT 1 FROM items WHERE id = :id FOR NO KEY UPDATE"), {"id": item.id}
        )
        adds = [
            asyncio.create_task(
                editor_client.post(f"/items/{item.id}/checklist", data={"text": name}, headers=HX)
            )
            for name in ("Eggs", "Flour", "Sugar")
        ]
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        responses = await asyncio.gather(*adds)

    assert [r.status_code for r in responses] == [200, 200, 200]
    positions = [position for _, position in await entries(db, item)]
    assert positions == [0, 1, 2]


async def test_a_stale_reorder_racing_a_delete_of_one_entry_is_422(editor_client, item, db):
    a, b, c = [
        await create_checklist_entry(db, item, name, position=position)
        for position, name in enumerate(("A", "B", "C"))
    ]
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("DELETE FROM checklist_entries WHERE id = :id"), {"id": b.id}
        )
        reorder = asyncio.create_task(
            editor_client.put(
                f"/items/{item.id}/checklist/order",
                data={"ids": [str(c.id), str(b.id), str(a.id)]},  # the order it saw, B included
                headers=HX,
            )
        )
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        r = await reorder

    assert r.status_code == 422 and toast(r) == CHECKLIST_OUT_OF_DATE
    assert await entries(db, item) == [("A", 0), ("C", 2)]  # nothing of the stale order applied


async def test_a_stale_reorder_racing_an_uncommitted_add_is_422(editor_client, item, db):
    a = await create_checklist_entry(db, item, "A", position=0)
    b = await create_checklist_entry(db, item, "B", position=1)
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(  # an add, half done: the item held, the entry inserted
            text("SELECT 1 FROM items WHERE id = :id FOR NO KEY UPDATE"), {"id": item.id}
        )
        await other_device.execute(
            text(
                "INSERT INTO checklist_entries (item_id, text, is_done, position)"
                " VALUES (:item, 'New', false, 2)"
            ),
            {"item": item.id},
        )
        reorder = asyncio.create_task(
            editor_client.put(
                f"/items/{item.id}/checklist/order",
                data={"ids": [str(b.id), str(a.id)]},  # without the new one
                headers=HX,
            )
        )
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        r = await reorder

    assert r.status_code == 422 and toast(r) == CHECKLIST_OUT_OF_DATE
    assert await entries(db, item) == [("A", 0), ("B", 1), ("New", 2)]


async def test_two_adds_at_99_entries_leave_100_with_distinct_positions(editor_client, item, db):
    db.add_all(
        ChecklistEntry(item_id=item.id, text=f"Step {n}", is_done=False, position=n)
        for n in range(MAX_ENTRIES - 1)
    )
    await db.commit()
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("SELECT 1 FROM items WHERE id = :id FOR NO KEY UPDATE"), {"id": item.id}
        )
        adds = [
            asyncio.create_task(
                editor_client.post(f"/items/{item.id}/checklist", data={"text": name}, headers=HX)
            )
            for name in ("One", "Two")
        ]
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        responses = await asyncio.gather(*adds)

    assert sorted(r.status_code for r in responses) == [200, 409]
    rows = await entries(db, item)
    assert len(rows) == MAX_ENTRIES
    assert len({position for _, position in rows}) == MAX_ENTRIES


async def test_a_reorder_waits_for_a_tick_of_another_entry_and_goes_through(
    editor_client, item, db
):
    a = await create_checklist_entry(db, item, "A", position=0)
    b = await create_checklist_entry(db, item, "B", position=1)
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            update(ChecklistEntry).where(ChecklistEntry.id == b.id).values(is_done=True)
        )
        reorder = asyncio.create_task(
            editor_client.put(
                f"/items/{item.id}/checklist/order",
                data={"ids": [str(b.id), str(a.id)]},
                headers=HX,
            )
        )
        other_pid = await other_device.scalar(text("SELECT pg_backend_pid()"))
        await wait_until_blocked_by(db, other_pid)
        await transaction.commit()
        r = await reorder

    assert r.status_code == 200
    assert await entries(db, item) == [("B", 0), ("A", 1)]
    assert await db.scalar(select(ChecklistEntry.is_done).where(ChecklistEntry.id == b.id)) is True
