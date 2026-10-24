"""An item's details for every role: who added it and when, from one route and in at most three
statements; an item that is gone, archived or deleted answers 404; and the board page holds a
details slot in the editor's modal and in the viewer's read-only dialog."""

import json
import re
from datetime import UTC, datetime

import pytest
from boards.test_item_views import attach
from factories import (
    create_attachment,
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, count_queries
from sqlalchemy import select, update

from content.test_board_markup import EDIT_CONTROLS
from easytodo.boards.service import Author
from easytodo.content.service import ITEM_GONE, _category_changed, get_item_details
from easytodo.models import Attachment, Board, BoardRole, Category, Item

HX = {"HX-Request": "true"}
TIME = '<time datetime="{}" class="whitespace-nowrap">{} UTC</time>'


async def dated(db, item: Item, created_at: datetime, updated_at: datetime) -> None:
    """Set the dates of ``item`` (``updated_at`` given, so the column's onupdate stays silent)."""
    await db.execute(
        update(Item).where(Item.id == item.id).values(created_at=created_at, updated_at=updated_at)
    )
    await db.commit()


async def test_details_show_who_added_the_item_and_both_dates(editor_client, category, db):
    anna = await create_user(db, "anna@example.com", display_name="Anna Kowalska")
    milk = await create_item(db, category, "Milk", created_by=anna)
    await dated(
        db, milk, datetime(2026, 10, 1, 8, 5, tzinfo=UTC), datetime(2026, 10, 2, 9, 15, tzinfo=UTC)
    )

    r = await editor_client.get(f"/items/{milk.id}/details", headers=HX)

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{milk.id}"' in r.text
    added = TIME.format("2026-10-01T08:05:00+00:00", "2026-10-01 08:05")
    assert "Added by Anna Kowalska · " + added in r.text
    assert "Modified " + TIME.format("2026-10-02T09:15:00+00:00", "2026-10-02 09:15") in r.text


async def test_details_without_an_author_say_only_when(editor_client, item, db):
    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert r.status_code == 200
    assert "Added <time" in r.text and "Added by" not in r.text  # nobody to name

    anna = await create_user(db, "anna@example.com")  # no display name: the email's first part
    await db.execute(update(Item).where(Item.id == item.id).values(created_by=anna.id))
    await db.commit()

    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert "Added by anna · <time" in r.text


async def test_one_member_board_shows_when_but_not_who(signed_in_client, db):
    sam = await create_user(db, "sam@example.com", display_name="Sam Smith")
    board = await create_board(db, sam, "Solo")
    category = await create_category(db, board, "Dairy")
    milk = await create_item(db, category, "Milk", created_by=sam)
    client = await signed_in_client("sam@example.com")

    r = await client.get(f"/items/{milk.id}/details", headers=HX)

    assert r.status_code == 200
    assert "Added <time" in r.text and "Modified <time" in r.text
    assert "Added by" not in r.text and "Sam Smith" not in r.text


async def test_viewer_gets_the_details_without_write_controls(viewer_client, item):
    r = await viewer_client.get(f"/items/{item.id}/details", headers=HX)

    assert r.status_code == 200
    assert f'<div id="item-details" data-details-item-id="{item.id}"' in r.text
    for control in EDIT_CONTROLS:
        assert control not in r.text, control
    assert "<form" not in r.text and "<input" not in r.text


@pytest.mark.parametrize("gone", ["deleted item", "archived category", "archived board"])
async def test_details_of_a_deleted_or_archived_item_are_404(editor_client, item, db, gone):
    now = datetime.now(UTC)
    target, column = {
        "deleted item": (Item, "deleted_at"),
        "archived category": (Category, "archived_at"),
        "archived board": (Board, "archived_at"),
    }[gone]
    row_id = {Item: item.id, Category: item.category_id, Board: item.board_id}[target]
    await db.execute(update(target).where(target.id == row_id).values({column: now}))
    await db.commit()

    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": ITEM_GONE},
        "resync": True,
    }
    assert r.text == ""


async def test_details_escape_the_authors_name(editor_client, category, db):  # Review Focus 3
    nasty = await create_user(db, "nasty@example.com", display_name=NASTY)
    milk = await create_item(db, category, "Milk", created_by=nasty)

    r = await editor_client.get(f"/items/{milk.id}/details", headers=HX)

    assert f"Added by {NASTY_ESCAPED} · " in r.text
    assert "<script>alert(1)</script>" not in r.text and "<b>" not in r.text


async def test_get_item_details_uses_at_most_three_queries(db):
    owner = await create_user(db, "owner@example.com")
    anna = await create_user(db, "anna@example.com", display_name="Anna Kowalska")
    board = await create_board(db, owner, "Groceries", {anna: BoardRole.EDITOR})
    category = await create_category(db, board, "Dairy")
    milk = await create_item(db, category, "Milk", created_by=anna)
    await create_checklist_entry(db, milk, "Flour", position=1)
    await create_checklist_entry(db, milk, "Eggs", position=0)
    attachment = await create_attachment(db, milk, filename="receipt-1.png", uploaded_by=anna)
    db.expunge_all()

    with count_queries(db) as statements:
        details = await get_item_details(db, milk.id, owner)

    assert len(statements) <= 3, statements
    assert details.item.id == milk.id
    assert details.role == BoardRole.OWNER
    assert details.show_author is True
    assert details.author == Author(anna.id, "Anna Kowalska")
    assert [entry.text for entry in details.entries] == ["Eggs", "Flour"]
    assert [v.attachment.id for v in details.attachments] == [attachment.id]
    assert [v.uploader for v in details.attachments] == [Author(anna.id, "Anna Kowalska")]


async def test_entries_and_attachments_come_in_their_order(db):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner, "Groceries")
    category = await create_category(db, board, "Dairy")
    milk = await create_item(db, category, "Milk")
    for text_, position in (("Salt", 1), ("Flour", 1), ("Eggs", 0)):  # a tie is broken by the id
        await create_checklist_entry(db, milk, text_, position=position)
    attach(db, milk, 1)
    attach(db, milk, 2)
    await db.flush()
    attachments = (await db.scalars(select(Attachment).order_by(Attachment.id))).all()
    attachments[0].created_at = datetime(2026, 10, 2, tzinfo=UTC)  # the first one is the later
    attachments[1].created_at = datetime(2026, 10, 1, tzinfo=UTC)
    await db.commit()
    db.expunge_all()

    details = await get_item_details(db, milk.id, owner)

    assert [entry.text for entry in details.entries] == ["Eggs", "Salt", "Flour"]
    assert [view.attachment.filename for view in details.attachments] == [
        "receipt-2.png",
        "receipt-1.png",
    ]
    assert [view.uploader for view in details.attachments] == [None, None]  # nobody uploaded them
    assert details.author is None and details.show_author is False


def test_category_changed_carries_item_ids_once():
    event = _category_changed(1, 7, 7, item_ids=(3, 3))

    assert (event.board_id, event.category_ids, event.item_ids) == (1, (7,), (3,))
    assert _category_changed(1, 7).item_ids == ()


async def test_board_page_has_a_details_dialog_for_each_role(
    editor_client, viewer_client, category, db
):
    nasty = await create_item(db, category, NASTY, description=NASTY)
    escaped = re.escape(NASTY_ESCAPED)
    slot = '<div id="item-details-slot" x-ref="details"></div>'

    editor = (await editor_client.get(f"/boards/{category.board_id}")).text

    assert re.search(rf"</form>\s*{slot}", editor)  # the slot is right after the form
    assert re.search(r'<input id="item-modal-name"[^>]* pattern="\.\*\\S\.\*"', editor)
    assert 'x-data="itemViewer"' not in editor

    viewer = (await viewer_client.get(f"/boards/{category.board_id}")).text

    assert 'x-data="itemViewer"' in viewer and slot in viewer
    assert 'aria-labelledby="item-viewer-title"' in viewer
    assert re.search(
        rf'id="item-{nasty.id}-name"[^>]* data-name="{escaped}" data-description="{escaped}"'
        r'[^>]* @click="openItem"',
        viewer,
    )
    assert 'x-data="itemModal"' not in viewer and 'name="description"' not in viewer
    dialog = viewer.partition('aria-labelledby="item-viewer-title"')[2].partition("</main>")[0]
    for field in ("<form", "<input", "<textarea", "<select", "hx-"):
        assert field not in dialog, field
