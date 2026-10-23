import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import wait_until_a_query_waits_for_a_lock
from sqlalchemy import delete, func, select, text, update

from easytodo.boards.service import list_boards
from easytodo.content.service import clear_done, get_category_view
from easytodo.content.validation import clean_description, clean_name
from easytodo.errors import ValidationFailed
from easytodo.models import Attachment, Board, BoardRole, Category, ChecklistEntry, Item, User
from easytodo.realtime.events import BoardEvent, EventType

HX = {"HX-Request": "true"}
ITEM_GONE = "This item no longer exists."
CATEGORY_GONE = "This category no longer exists."


def toast(response) -> str:
    return json.loads(response.headers["hx-trigger"])["toast"]["message"]


def changed(board_id: int, *category_ids: int) -> BoardEvent:
    return BoardEvent(EventType.CATEGORY_CHANGED, board_id=board_id, category_ids=category_ids)


def item_ids_in(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'id="item-(\d+)"', html)]


async def item_row(db, item_id: int) -> dict:
    """Every column of the item's row (the author, ``updated_at`` and ``deleted_at`` too), and how
    many checklist entries and attachments it has."""
    entries = select(func.count()).where(ChecklistEntry.item_id == Item.id).scalar_subquery()
    attachments = select(func.count()).where(Attachment.item_id == Item.id).scalar_subquery()
    row = await db.execute(
        select(*Item.__table__.c, entries.label("entries"), attachments.label("attachments")).where(
            Item.id == item_id
        )
    )
    return dict(row.one()._mapping)


def resync_with(message: str) -> dict:
    return {"toast": {"level": "error", "message": message}, "resync": True}


async def test_items_sorted_open_first_then_alphabetically(db):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner)
    fruit = await create_category(db, board, "Fruit")
    await create_item(db, fruit, "banana", is_done=True)
    await create_item(db, fruit, "Apple")
    await create_item(db, fruit, "cherry")
    await create_item(db, await create_category(db, board, "Other", position=1), "Elsewhere")

    view, role = await get_category_view(db, fruit.id, owner)

    assert (view.category.id, role) == (fruit.id, BoardRole.OWNER)
    assert [i.name for i in view.items] == ["Apple", "cherry", "banana"]


async def test_create_item_returns_the_category_fragment(
    editor_client, board, category, db, published
):
    await create_item(db, category, "Butter")

    r = await editor_client.post(
        f"/categories/{category.id}/items",
        data={"name": "  Oat   milk ", "description": "  Two cartons\r\nthe green ones  "},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    item = await db.scalar(select(Item).where(Item.name == "Oat milk"))
    assert (item.board_id, item.category_id) == (board.id, category.id)
    assert item.description == "Two cartons\nthe green ones"
    assert (item.is_done, item.done_at) == (False, None)
    assert f'id="category-{category.id}"' in r.text
    assert item_ids_in(r.text)[0] != item.id  # "Butter" comes first
    assert item.id in item_ids_in(r.text) and "Two cartons" in r.text
    assert published == [((changed(board.id, category.id),), "tab-1")]

    r = await editor_client.post(f"/categories/{category.id}/items", data={"name": "Eggs"})
    assert r.status_code == 200
    assert await db.scalar(select(Item.description).where(Item.name == "Eggs")) is None


async def test_update_item_changes_name_and_description(editor_client, board, item, db, published):
    r = await editor_client.patch(
        f"/items/{item.id}",
        data={"name": " Whole milk ", "description": "3.2% fat"},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert f'id="category-{item.category_id}"' in r.text and "Whole milk" in r.text
    row = (await db.execute(select(Item.name, Item.description))).one()
    assert tuple(row) == ("Whole milk", "3.2% fat")
    assert published == [((changed(board.id, item.category_id),), "tab-1")]

    r = await editor_client.patch(f"/items/{item.id}", data={"name": "Milk", "description": "  "})
    assert r.status_code == 200
    assert tuple((await db.execute(select(Item.name, Item.description))).one()) == ("Milk", None)


async def test_toggle_sets_and_clears_done_at(editor_client, board, item, db, published):
    later = await create_item(db, await db.get_one(Category, item.category_id), "Yoghurt")

    r = await editor_client.post(f"/items/{item.id}/toggle", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    is_done, done_at = (
        await db.execute(select(Item.is_done, Item.done_at).where(Item.id == item.id))
    ).one()
    assert is_done is True
    assert abs(datetime.now(UTC) - done_at) < timedelta(minutes=1)
    assert f'id="category-{item.category_id}"' in r.text
    assert item_ids_in(r.text) == [later.id, item.id]  # done items go last
    assert re.search(rf'id="item-{item.id}"[^>]*line-through', r.text)
    assert published == [((changed(board.id, item.category_id),), "tab-1")]

    r = await editor_client.post(f"/items/{item.id}/toggle")

    assert r.status_code == 200
    row = (await db.execute(select(Item.is_done, Item.done_at).where(Item.id == item.id))).one()
    assert tuple(row) == (False, None)
    assert item_ids_in(r.text) == [item.id, later.id]  # "Milk" before "Yoghurt" again


async def test_toggle_with_the_wanted_state_is_idempotent(
    editor_client, board, item, db, published
):
    """Two devices tick the same item, one of them from a stale view: both meant "done", so it
    stays done (the checkbox sends the state its user wants, not "flip it")."""
    for _ in range(2):
        r = await editor_client.post(f"/items/{item.id}/toggle", data={"done": "true"}, headers=HX)
        assert r.status_code == 200
        assert re.search(rf'id="item-{item.id}"[^>]*line-through', r.text)
    is_done, done_at = (
        await db.execute(select(Item.is_done, Item.done_at).where(Item.id == item.id))
    ).one()
    assert is_done is True and done_at is not None
    events = [events for events, _ in published if events]
    assert len(events) == 1  # the second tick changed nothing, so nothing to tell

    for _ in range(2):
        r = await editor_client.post(f"/items/{item.id}/toggle", data={"done": "false"}, headers=HX)
        assert r.status_code == 200
    row = (await db.execute(select(Item.is_done, Item.done_at).where(Item.id == item.id))).one()
    assert tuple(row) == (False, None)
    assert len([events for events, _ in published if events]) == 2

    r = await editor_client.post(f"/items/{item.id}/toggle", data={"done": "yes"}, headers=HX)
    assert r.status_code == 422


async def test_moving_an_item_to_its_own_category_changes_nothing(
    editor_client, board, item, db, published
):
    """No row lock on the category it is in already, which could deadlock against a delete or
    "Clear done" of that category, and no event."""
    r = await editor_client.post(
        f"/items/{item.id}/move", data={"category_id": item.category_id}, headers=HX
    )

    assert r.status_code == 200
    assert f'id="item-{item.id}"' in r.text
    assert await db.scalar(select(Item.category_id).where(Item.id == item.id)) == item.category_id
    assert [events for events, _ in published if events] == []


async def test_move_item_to_another_category_of_the_board(
    editor_client, board, category, item, db, published
):
    bakery = await create_category(db, board, "Bakery", position=1)

    r = await editor_client.post(
        f"/items/{item.id}/move",
        data={"category_id": bakery.id},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert f'id="board-content" data-board-id="{board.id}"' in r.text
    row = (await db.execute(select(Item.category_id, Item.board_id))).one()
    assert tuple(row) == (bakery.id, board.id)
    bakery_html = r.text[r.text.index(f'id="category-{bakery.id}"') :]
    assert item_ids_in(bakery_html) == [item.id]
    assert published == [((changed(board.id, category.id, bakery.id),), "tab-1")]

    published.clear()
    r = await editor_client.post(f"/items/{item.id}/move", data={"category_id": bakery.id})
    assert r.status_code == 200
    assert [events for events, _ in published if events] == []  # there already: no change


async def test_delete_item_hides_it_and_offers_undo(
    editor_client, board, category, item, db, published
):
    butter = await create_item(db, category, "Butter")

    r = await editor_client.delete(f"/items/{item.id}", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text
    assert item_ids_in(r.text) == [butter.id]
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {
            "level": "info",
            "message": "Item deleted.",
            "undo": {"url": f"/items/{item.id}/restore", "swap_into": f"#category-{category.id}"},
        }
    }
    # The row stays, marked as deleted just now; "Milk" is on none of the pages.
    deleted_at = await db.scalar(select(Item.deleted_at).where(Item.id == item.id))
    assert abs(datetime.now(UTC) - deleted_at) < timedelta(minutes=1)
    for path in (
        f"/boards/{board.id}",
        f"/boards/{board.id}/content",
        f"/categories/{category.id}",
    ):
        page = await editor_client.get(path)
        assert item_ids_in(page.text) == [butter.id], path
        assert "Milk" not in page.text, path
    owner = await db.get_one(User, board.owner_id)
    assert [summary.open_items for summary in await list_boards(db, owner)] == [1]
    assert published == [((changed(board.id, category.id),), "tab-1")]


async def test_undo_restores_the_item_unchanged(editor_client, board, category, db, published):
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    milk = await create_item(
        db, category, "Milk", is_done=True, created_by=editor, description="2 litres"
    )
    await create_checklist_entry(db, milk, "Skimmed")
    db.add(
        Attachment(
            item_id=milk.id,
            filename="receipt.png",
            kind="image",
            content_type="image/png",
            size_bytes=12,
            storage_key="a" * 32,
            uploaded_by=editor.id,
        )
    )
    # Long ago, to show that neither the delete nor the undo touches it.
    await db.execute(
        update(Item)
        .where(Item.id == milk.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=3))
    )
    await db.commit()
    before = await item_row(db, milk.id)
    assert (before["created_by"], before["is_done"], before["description"]) == (
        editor.id,
        True,
        "2 litres",
    )
    assert (before["entries"], before["attachments"]) == (1, 1)

    r = await editor_client.delete(f"/items/{milk.id}", headers=HX)
    assert r.status_code == 200
    gone = await item_row(db, milk.id)
    assert gone["deleted_at"] is not None
    assert gone | {"deleted_at": None} == before  # nothing else changes, updated_at included
    assert (gone["entries"], gone["attachments"]) == (1, 1)  # kept for the undo
    published.clear()

    r = await editor_client.post(f"/items/{milk.id}/restore", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text
    assert item_ids_in(r.text) == [milk.id]
    assert await item_row(db, milk.id) == before  # also updated_at
    assert published == [((changed(board.id, category.id),), "tab-1")]

    r = await editor_client.post(f"/items/{milk.id}/restore", headers=HX)  # not deleted any more
    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == resync_with(ITEM_GONE)
    assert await item_row(db, milk.id) == before


@pytest.mark.parametrize(("seconds_ago", "status"), [(59, 200), (61, 404)])
async def test_undo_works_only_for_60_seconds(editor_client, item, db, seconds_ago, status):
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    deleted_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    await db.execute(update(Item).where(Item.id == item.id).values(deleted_at=deleted_at))
    await db.commit()

    r = await editor_client.post(f"/items/{item.id}/restore", headers=HX)

    assert r.status_code == status
    if status == 404:
        assert json.loads(r.headers["hx-trigger"]) == resync_with(ITEM_GONE)
        assert await db.scalar(select(Item.deleted_at).where(Item.id == item.id)) == deleted_at
    else:
        assert item_ids_in(r.text) == [item.id]
        assert await db.scalar(select(Item.deleted_at).where(Item.id == item.id)) is None


@pytest.mark.parametrize("archived", ["category", "board"])
async def test_undo_after_the_category_was_archived_is_404(  # Review Focus 4
    editor_client, board, category, item, db, archived
):
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    if archived == "category":
        r = await editor_client.post(f"/categories/{category.id}/archive", headers=HX)
        assert r.status_code == 200
    else:
        await db.execute(update(Board).where(Board.id == board.id).values(archived_at=func.now()))
        await db.commit()
    deleted_at = await db.scalar(select(Item.deleted_at).where(Item.id == item.id))
    assert deleted_at is not None

    r = await editor_client.post(f"/items/{item.id}/restore", headers=HX)

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == resync_with(ITEM_GONE)
    assert await db.scalar(select(Item.deleted_at).where(Item.id == item.id)) == deleted_at


async def test_actions_on_a_soft_deleted_item_are_404_with_resync(  # Review Focus 4
    editor_client, item, db, content_snapshot
):
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    before = await content_snapshot()
    assert [row for row in before[1] if row.deleted_at is not None] != []

    for method, path, data in (
        ("POST", f"/items/{item.id}/toggle", None),
        ("POST", f"/items/{item.id}/toggle", {"done": "true"}),
        ("PATCH", f"/items/{item.id}", {"name": "Oat milk", "description": ""}),
        ("POST", f"/items/{item.id}/move", {"category_id": item.category_id}),
        ("DELETE", f"/items/{item.id}", None),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path, data)
        assert json.loads(r.headers["hx-trigger"]) == resync_with(ITEM_GONE), (method, path)

    assert await content_snapshot() == before


async def test_viewer_gets_404_for_a_soft_deleted_item_not_403(  # Review Focus 4
    editor_client, viewer_client, item, db, content_snapshot
):
    """The unlocked lookup that tells a missing row from a missing permission leaves the deleted
    item out too: a viewer is not told there is one."""
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    before = await content_snapshot()

    for method, path, data in (
        ("POST", f"/items/{item.id}/toggle", None),
        ("PATCH", f"/items/{item.id}", {"name": "Oat milk"}),
        ("POST", f"/items/{item.id}/move", {"category_id": item.category_id}),
        ("DELETE", f"/items/{item.id}", None),
    ):
        r = await viewer_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == resync_with(ITEM_GONE), (method, path)

    assert await content_snapshot() == before


@pytest.mark.parametrize("seconds_ago", [0, 120])
async def test_viewer_cannot_undo(editor_client, viewer_client, item, db, seconds_ago):
    """403 whether the window is open or not: the role is checked before the clock."""
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    deleted_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    await db.execute(update(Item).where(Item.id == item.id).values(deleted_at=deleted_at))
    await db.commit()

    r = await viewer_client.post(f"/items/{item.id}/restore", headers=HX)

    assert r.status_code == 403
    assert toast(r) == "You don't have permission to do that."
    assert await db.scalar(select(Item.deleted_at).where(Item.id == item.id)) == deleted_at


async def test_clear_done_leaves_soft_deleted_items_for_undo(editor_client, board, category, db):
    old_milk = await create_item(db, category, "Old milk", is_done=True)
    await create_item(db, category, "Cheese", is_done=True)
    await editor_client.delete(f"/items/{old_milk.id}", headers=HX)
    owner = await db.get_one(User, board.owner_id)

    assert await clear_done(db, category.id, owner) == 1  # "Cheese" only

    r = await editor_client.post(f"/items/{old_milk.id}/restore", headers=HX)
    assert r.status_code == 200
    assert item_ids_in(r.text) == [old_milk.id]


async def test_clear_done_removes_only_done_items(editor_client, board, category, db, published):
    await create_item(db, category, "Milk", is_done=True)
    butter = await create_item(db, category, "Butter")
    await create_item(db, category, "Cheese", is_done=True)
    fruit = await create_category(db, board, "Fruit", position=1)
    old_apples = await create_item(db, fruit, "Old apples", is_done=True)

    r = await editor_client.post(
        f"/categories/{category.id}/clear-done", headers=HX | {"X-Client-Id": "tab-1"}
    )

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text
    assert item_ids_in(r.text) == [butter.id]
    assert set(await db.scalars(select(Item.id))) == {butter.id, old_apples.id}
    assert published == [((changed(board.id, category.id),), "tab-1")]

    published.clear()
    r = await editor_client.post(f"/categories/{category.id}/clear-done")
    assert r.status_code == 200
    assert not any(events for events, _ in published)  # nothing removed, nothing to announce

    await create_item(db, category, "Eggs", is_done=True)
    await create_item(db, category, "Cream", is_done=True)
    owner = await db.get_one(User, board.owner_id)
    assert await clear_done(db, category.id, owner) == 2


async def test_action_on_archived_category_returns_404_with_resync(editor_client, category):
    r = await editor_client.post(f"/categories/{category.id}/archive")
    assert r.status_code == 200
    for method, path, data in (
        ("GET", f"/categories/{category.id}", None),
        ("PATCH", f"/categories/{category.id}", {"name": "Dairy"}),
        ("POST", f"/categories/{category.id}/archive", None),
        ("POST", f"/categories/{category.id}/items", {"name": "Milk"}),
        ("POST", f"/categories/{category.id}/clear-done", None),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": CATEGORY_GONE},
            "resync": True,
        }


@pytest.mark.parametrize(
    "deletion,method,path,data,message",
    [
        ("item", "POST", "/items/{item}/toggle", None, ITEM_GONE),
        ("item", "PATCH", "/items/{item}", {"name": "Oat milk"}, ITEM_GONE),
        ("soft", "POST", "/items/{item}/toggle", None, ITEM_GONE),  # Review Focus 4
        ("category", "POST", "/categories/{category}/items", {"name": "Eggs"}, CATEGORY_GONE),
        ("category", "POST", "/items/{item}/move", {"category_id": "{category}"}, CATEGORY_GONE),
        ("archive", "POST", "/categories/{category}/items", {"name": "Eggs"}, CATEGORY_GONE),
        ("archive", "POST", "/items/{item}/move", {"category_id": "{category}"}, CATEGORY_GONE),
        ("archive", "POST", "/categories/{category}/archive", None, CATEGORY_GONE),
    ],
)
async def test_change_racing_a_delete_on_another_device_is_404_not_500(  # Review Focus 2
    editor_client, board, category, item, db, deletion, method, path, data, message
):
    """The other device's delete (or soft delete of the item, or archiving of the category) holds
    its row lock until it commits; the change waits for it and then finds the row gone."""
    source = await create_category(db, board, "Source", position=1)
    moving = await create_item(db, source, "Moving")
    ids = {
        "item": item.id if deletion in ("item", "soft") else moving.id,
        "category": category.id,
    }
    if data is not None:
        data = {k: v.format(**ids) for k, v in data.items()}
    deleted = delete(Item).where(Item.id == item.id)
    if deletion == "soft":
        deleted = update(Item).where(Item.id == item.id).values(deleted_at=func.now())
    elif deletion == "category":
        deleted = delete(Category).where(Category.id == category.id)
    elif deletion == "archive":
        deleted = update(Category).where(Category.id == category.id).values(archived_at=func.now())

    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(deleted)
        change = asyncio.create_task(
            editor_client.request(method, path.format(**ids), data=data, headers=HX)
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await change

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": message},
        "resync": True,
    }


@pytest.mark.parametrize(
    "held_row,held,method,path,data",
    [
        # The category is only the parent of a new item, of the done items "Clear done" deletes,
        # or of an item moved into it.
        ("category", "FOR SHARE", "POST", "/categories/{category}/items", {"name": "Eggs"}),
        ("category", "FOR SHARE", "POST", "/categories/{category}/clear-done", None),
        (
            "other_category",
            "FOR SHARE",
            "POST",
            "/items/{item}/move",
            {"category_id": "{other_category}"},
        ),
        # A rename, an archiving, a tick or a soft delete changes no key: a row a new child row
        # points to (its foreign key check holds FOR KEY SHARE) can still change.
        ("category", "FOR KEY SHARE", "PATCH", "/categories/{category}", {"name": "Fridge"}),
        ("category", "FOR KEY SHARE", "POST", "/categories/{category}/archive", None),
        ("item", "FOR KEY SHARE", "POST", "/items/{item}/toggle", None),
        ("item", "FOR KEY SHARE", "DELETE", "/items/{item}", None),
    ],
)
async def test_writes_take_only_the_row_lock_they_need(
    editor_client, board, category, item, db, held_row, held, method, path, data
):
    """Another device holds a lock on the row that does not conflict: the write does not wait."""
    other_category = await create_category(db, board, "Bakery", position=1)
    ids = {"category": category.id, "item": item.id, "other_category": other_category.id}
    table = "items" if held_row == "item" else "categories"
    if data is not None:
        data = {key: value.format(**ids) for key, value in data.items()}
    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id {held}"), {"id": ids[held_row]}
        )
        request = editor_client.request(method, path.format(**ids), data=data, headers=HX)
        r = await asyncio.wait_for(request, 2)

    assert r.status_code == 200, r.headers.get("hx-trigger")


async def test_undo_takes_only_the_row_lock_it_needs(editor_client, item, db):
    """As the soft delete: a checklist entry or an attachment being added to the item (its foreign
    key check holds FOR KEY SHARE) does not hold up the undo."""
    await editor_client.delete(f"/items/{item.id}", headers=HX)
    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text("SELECT 1 FROM items WHERE id = :id FOR KEY SHARE"), {"id": item.id}
        )
        request = editor_client.post(f"/items/{item.id}/restore", headers=HX)
        r = await asyncio.wait_for(request, 2)

    assert r.status_code == 200, r.headers.get("hx-trigger")


async def test_viewer_write_is_403_without_waiting_for_a_row_lock(viewer_client, item, db):
    """An editor's change holds the item and its category locked: a viewer's write is refused at
    once instead of queueing behind it (and, had it got there first, would hold up the editor)."""
    async with db.bind.connect() as other_device, other_device.begin():
        for table, row_id in (("categories", item.category_id), ("items", item.id)):
            await other_device.execute(
                text(f"SELECT 1 FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id}
            )
        for method, path, data in (
            ("POST", f"/items/{item.id}/toggle", None),
            ("PATCH", f"/items/{item.id}", {"name": "Oat milk"}),
            ("DELETE", f"/items/{item.id}", None),
            ("POST", f"/items/{item.id}/restore", None),
            ("POST", f"/categories/{item.category_id}/items", {"name": "Eggs"}),
            ("POST", f"/categories/{item.category_id}/clear-done", None),
            ("PATCH", f"/categories/{item.category_id}", {"name": "Fridge"}),
            ("POST", f"/categories/{item.category_id}/archive", None),
            ("POST", f"/categories/{item.category_id}/restore", None),
            ("DELETE", f"/categories/{item.category_id}", None),
        ):
            request = viewer_client.request(method, path, data=data, headers=HX)
            r = await asyncio.wait_for(request, 2)
            assert r.status_code == 403, (method, path)
            assert toast(r) == "You don't have permission to do that."


async def test_name_length_limits(editor_client, category):
    for data, message in (
        ({"name": "x" * 201}, "Item name must be 1–200 characters."),
        ({"name": "  \t "}, "Item name must be 1–200 characters."),
        ({}, "Item name must be 1–200 characters."),
        (
            {"name": "Milk", "description": "x" * 2001},
            "Description must be at most 2000 characters.",
        ),
    ):
        r = await editor_client.post(f"/categories/{category.id}/items", data=data, headers=HX)
        assert r.status_code == 422, data
        assert toast(r) == message

    r = await editor_client.post(
        f"/categories/{category.id}/items", data={"name": "x" * 200, "description": "y" * 2000}
    )
    assert r.status_code == 200
    item_id = item_ids_in(r.text)[0]

    r = await editor_client.patch(f"/items/{item_id}", data={"name": "x" * 201}, headers=HX)
    assert r.status_code == 422
    assert toast(r) == "Item name must be 1–200 characters."
    r = await editor_client.patch(
        f"/items/{item_id}", data={"name": "Milk", "description": "y" * 2001}, headers=HX
    )
    assert r.status_code == 422
    assert toast(r) == "Description must be at most 2000 characters."


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  Oat \t\n milk  ", "Oat milk"),
        ("Milk\x00", "Milk"),  # PostgreSQL text cannot hold NUL
        ("x" * 10, "x" * 10),
    ],
)
def test_clean_name_strips_and_collapses_whitespace(value, expected):
    assert clean_name(value, max_len=10, label="Thing") == expected


@pytest.mark.parametrize("value", ["", " \t\n ", "\x00", "x" * 11])
def test_clean_name_refuses_empty_and_long_names(value):
    with pytest.raises(ValidationFailed, match="^Thing must be 1–10 characters.$"):
        clean_name(value, max_len=10, label="Thing")


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("  \r\n ", None),
        ("  Two cartons\r\n  the green ones\x00 ", "Two cartons\n  the green ones"),
        ("\r\n".join(["y"] * 1000), "\n".join(["y"] * 1000)),  # 2998 characters sent, 1999 kept
    ],
)
def test_clean_description_strips_and_keeps_line_breaks(value, expected):
    assert clean_description(value) == expected


def test_clean_description_refuses_more_than_2000_characters():
    assert clean_description("y" * 2000) == "y" * 2000
    with pytest.raises(ValidationFailed, match="^Description must be at most 2000 characters.$"):
        clean_description("y" * 2001)
