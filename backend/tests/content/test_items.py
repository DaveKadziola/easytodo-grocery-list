import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from factories import create_board, create_category, create_item, create_user
from sqlalchemy import delete, select, text

from easytodo.content.service import clear_done, get_category_view
from easytodo.content.validation import clean_description, clean_name
from easytodo.errors import ValidationFailed
from easytodo.models import BoardRole, Category, Item, User
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


async def test_delete_item_returns_the_category_without_it(
    editor_client, board, category, item, db, published
):
    butter = await create_item(db, category, "Butter")

    r = await editor_client.delete(f"/items/{item.id}", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text
    assert item_ids_in(r.text) == [butter.id]
    assert list(await db.scalars(select(Item.id))) == [butter.id]
    assert published == [((changed(board.id, category.id),), "tab-1")]


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


# Review Focus 2
async def test_action_on_deleted_item_returns_404_with_resync(editor_client, item, db):
    await editor_client.delete(f"/items/{item.id}")
    r = await editor_client.post(f"/items/{item.id}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": "This item no longer exists."},
        "resync": True,
    }

    for method, path, data in (
        ("PATCH", f"/items/{item.id}", {"name": "Milk", "description": ""}),
        ("POST", f"/items/{item.id}/move", {"category_id": item.category_id}),
        ("DELETE", f"/items/{item.id}", None),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": ITEM_GONE},
            "resync": True,
        }


async def test_action_on_deleted_category_returns_404_with_resync(editor_client, category):
    await editor_client.delete(f"/categories/{category.id}")
    for method, path, data in (
        ("GET", f"/categories/{category.id}", None),
        ("PATCH", f"/categories/{category.id}", {"name": "Dairy"}),
        ("DELETE", f"/categories/{category.id}", None),
        ("POST", f"/categories/{category.id}/items", {"name": "Milk"}),
        ("POST", f"/categories/{category.id}/clear-done", None),
    ):
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": CATEGORY_GONE},
            "resync": True,
        }


async def _wait_until_a_query_waits_for_a_lock(db) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with db.bind.connect() as conn:  # a fresh snapshot of pg_stat_activity each time
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no query started waiting for the row lock")


@pytest.mark.parametrize(
    "deletion,method,path,data,message",
    [
        ("item", "POST", "/items/{item}/toggle", None, ITEM_GONE),
        ("item", "PATCH", "/items/{item}", {"name": "Oat milk"}, ITEM_GONE),
        ("category", "POST", "/categories/{category}/items", {"name": "Eggs"}, CATEGORY_GONE),
        ("category", "POST", "/items/{item}/move", {"category_id": "{category}"}, CATEGORY_GONE),
    ],
)
async def test_change_racing_a_delete_on_another_device_is_404_not_500(  # Review Focus 2
    editor_client, board, category, item, db, deletion, method, path, data, message
):
    """The other device's delete holds its row lock until it commits; the change waits for it and
    then finds the row gone."""
    source = await create_category(db, board, "Source", position=1)
    moving = await create_item(db, source, "Moving")
    ids = {"item": item.id if deletion == "item" else moving.id, "category": category.id}
    if data is not None:
        data = {k: v.format(**ids) for k, v in data.items()}
    deleted = delete(Item).where(Item.id == item.id)
    if deletion == "category":
        deleted = delete(Category).where(Category.id == category.id)

    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(deleted)
        change = asyncio.create_task(
            editor_client.request(method, path.format(**ids), data=data, headers=HX)
        )
        await _wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await change

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": message},
        "resync": True,
    }


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
