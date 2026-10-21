import json
import re

import pytest
from factories import create_board, create_category, create_item, create_user
from sqlalchemy import delete, select

from easytodo.models import Category, Item
from easytodo.realtime.events import BoardEvent, EventType

HX = {"HX-Request": "true"}
DUPLICATE = "A category with this name already exists."
OUT_OF_DATE = "Category order is out of date. Refresh the board."
CATEGORY_GONE = "This category no longer exists."


def toast(response) -> str:
    return json.loads(response.headers["hx-trigger"])["toast"]["message"]


async def categories_in_order(db, board) -> list[str]:
    names = await db.scalars(
        select(Category.name)
        .where(Category.board_id == board.id)
        .order_by(Category.position, Category.id)
    )
    return list(names)


async def test_create_category_returns_its_fragment_and_goes_last(
    editor_client, board, db, published
):
    r = await editor_client.post(
        f"/boards/{board.id}/categories",
        data={"name": "  Fresh \t fruit  "},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 201
    first = await db.scalar(select(Category))
    assert (first.name, first.position) == ("Fresh fruit", 0)
    assert f'id="category-{first.id}"' in r.text
    assert f'data-category-id="{first.id}"' in r.text
    assert "Fresh fruit" in r.text
    assert published == [
        (
            (BoardEvent(EventType.CATEGORY_CHANGED, board_id=board.id, category_ids=(first.id,)),),
            "tab-1",
        )
    ]

    await create_category(db, board, "Bakery", position=7)
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": "Frozen"})
    assert r.status_code == 201
    assert await db.scalar(select(Category.position).where(Category.name == "Frozen")) == 8
    assert await categories_in_order(db, board) == ["Fresh fruit", "Bakery", "Frozen"]


async def test_duplicate_category_name_is_409_case_insensitive(editor_client, board, db):
    await create_category(db, board, "Dairy", position=0)
    fruit = await create_category(db, board, "Fruit", position=1)
    other_board = await create_board(db, await create_user(db, "someone@example.com"), "Other")
    await create_category(db, other_board, "Bakery")

    r = await editor_client.post(
        f"/boards/{board.id}/categories", data={"name": " DAIRY "}, headers=HX
    )
    assert r.status_code == 409
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": DUPLICATE}
    }

    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": "dairy"})
    assert r.status_code == 409  # without HTMX: the error page
    assert DUPLICATE in r.text

    r = await editor_client.patch(f"/categories/{fruit.id}", data={"name": "dAiRy"}, headers=HX)
    assert r.status_code == 409
    assert toast(r) == DUPLICATE

    assert await categories_in_order(db, board) == ["Dairy", "Fruit"]
    # The same name on another board is fine, and so is a category keeping its own name.
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": "bakery"})
    assert r.status_code == 201
    r = await editor_client.patch(f"/categories/{fruit.id}", data={"name": "FRUIT"})
    assert r.status_code == 200
    assert await categories_in_order(db, board) == ["Dairy", "FRUIT", "bakery"]


async def test_reorder_requires_exact_permutation(editor_client, board, db, published):
    a, b, c = [await create_category(db, board, name, position=i) for i, name in enumerate("ABC")]
    foreign = await create_category(
        db, await create_board(db, await create_user(db, "someone@example.com"), "Other"), "X"
    )

    for ids in (
        [a.id, b.id],  # one missing
        [a.id, b.id, c.id, foreign.id],  # one extra, from another board
        [a.id, b.id, foreign.id],  # one swapped for another board's
        [a.id, a.id, b.id],  # repeated
        [a.id, b.id, c.id, c.id],
        [a.id, b.id, c.id, 2**63],
        [],
    ):
        r = await editor_client.put(
            f"/boards/{board.id}/categories/order", data={"ids": ids}, headers=HX
        )
        assert r.status_code == 422, ids
        assert toast(r) == OUT_OF_DATE
    assert await categories_in_order(db, board) == ["A", "B", "C"]
    assert published == []

    r = await editor_client.put(
        f"/boards/{board.id}/categories/order",
        data={"ids": [c.id, a.id, b.id]},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 204
    assert r.content == b""
    assert await categories_in_order(db, board) == ["C", "A", "B"]
    positions = await db.scalars(
        select(Category.position).where(Category.board_id == board.id).order_by(Category.position)
    )
    assert list(positions) == [0, 1, 2]
    assert published == [
        ((BoardEvent(EventType.CATEGORIES_REORDERED, board_id=board.id),), "tab-1")
    ]


async def test_rename_category_returns_the_fragment_with_its_items(
    editor_client, board, category, item, db, published
):
    r = await editor_client.patch(
        f"/categories/{category.id}",
        data={"name": "  Milk   and cheese "},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text
    assert "Milk and cheese" in r.text
    assert f'id="item-{item.id}"' in r.text
    assert await db.scalar(select(Category.name)) == "Milk and cheese"
    assert published == [
        (
            (
                BoardEvent(
                    EventType.CATEGORY_CHANGED, board_id=board.id, category_ids=(category.id,)
                ),
            ),
            "tab-1",
        )
    ]


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
async def test_category_name_must_have_1_to_120_characters(
    editor_client, board, category, db, name
):
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": name}, headers=HX)
    assert r.status_code == 422
    assert toast(r) == "Category name must be 1–120 characters."

    r = await editor_client.patch(f"/categories/{category.id}", data={"name": name}, headers=HX)
    assert r.status_code == 422
    assert toast(r) == "Category name must be 1–120 characters."
    assert await categories_in_order(db, board) == ["Dairy"]


async def test_category_name_may_have_120_characters(editor_client, board, db):
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": "x" * 120})
    assert r.status_code == 201
    assert await categories_in_order(db, board) == ["x" * 120]


async def test_delete_category_removes_it_with_its_items(
    editor_client, board, category, item, db, published
):
    keep = await create_category(db, board, "Bakery", position=1)
    bread = await create_item(db, keep, "Bread")

    r = await editor_client.delete(
        f"/categories/{category.id}", headers=HX | {"X-Client-Id": "tab-1"}
    )

    assert r.status_code == 200
    assert r.content == b""
    assert await categories_in_order(db, board) == ["Bakery"]
    assert list(await db.scalars(select(Item.id))) == [bread.id]
    assert published == [
        (
            (
                BoardEvent(
                    EventType.CATEGORY_DELETED, board_id=board.id, category_ids=(category.id,)
                ),
            ),
            "tab-1",
        )
    ]
    r = await editor_client.get(f"/categories/{category.id}", headers=HX)
    assert r.status_code == 404
    assert toast(r) == CATEGORY_GONE


async def test_category_fragment_lists_its_items_open_first(editor_client, board, category, db):
    for name, done in (("banana", True), ("Apple", False), ("cherry", False)):
        await create_item(db, category, name, is_done=done)
    await create_item(db, await create_category(db, board, "Other", position=1), "Elsewhere")

    r = await editor_client.get(f"/categories/{category.id}")

    assert r.status_code == 200
    assert f'id="category-{category.id}"' in r.text and "Dairy" in r.text
    assert "Elsewhere" not in r.text
    ids = [int(i) for i in re.findall(r'id="item-(\d+)"', r.text)]
    names = {i: n for i, n in (await db.execute(select(Item.id, Item.name))).all()}
    assert [names[i] for i in ids] == ["Apple", "cherry", "banana"]


async def test_board_content_and_page_show_categories_by_position_with_dom_ids(
    editor_client, board, db
):
    dairy = await create_category(db, board, "Dairy", position=1)
    fruit = await create_category(db, board, "Fruit", position=0)
    milk = await create_item(db, dairy, "Milk")
    apple = await create_item(db, fruit, "Apple")

    for path in (f"/boards/{board.id}/content", f"/boards/{board.id}"):
        r = await editor_client.get(path)
        assert r.status_code == 200
        assert f'id="board-content" data-board-id="{board.id}"' in r.text
        assert re.findall(r'id="category-(\d+)"', r.text) == [str(fruit.id), str(dairy.id)]
        assert f'data-category-id="{dairy.id}"' in r.text
        assert re.findall(r'id="item-(\d+)"', r.text) == [str(apple.id), str(milk.id)]
        assert f'data-item-id="{milk.id}"' in r.text


async def test_names_are_rendered_escaped(editor_client, board):  # Review Focus 1
    nasty = 'O\'Reilly <script>alert(1)</script> "x"'
    escaped = "O&#39;Reilly &lt;script&gt;alert(1)&lt;/script&gt; &#34;x&#34;"

    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": nasty})
    assert r.status_code == 201
    assert (
        "<script>alert(1)</script>" not in r.text
        and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    )
    category_id = int(re.search(r'id="category-(\d+)"', r.text)[1])
    r = await editor_client.post(
        f"/categories/{category_id}/items", data={"name": nasty, "description": nasty}
    )
    assert r.status_code == 200
    item_id = int(re.search(r'id="item-(\d+)"', r.text)[1])

    responses = [
        r,
        await editor_client.get(f"/categories/{category_id}"),
        await editor_client.get(f"/boards/{board.id}/content"),
        await editor_client.get(f"/boards/{board.id}"),
        await editor_client.post(f"/items/{item_id}/toggle"),
        await editor_client.patch(f"/items/{item_id}", data={"name": nasty, "description": nasty}),
    ]
    for r in responses:
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert r.text.count(escaped) >= 3  # category name, item name and item description


async def test_board_deleted_while_a_category_is_created_is_a_404_not_a_500(
    editor_client, board, db, monkeypatch, _db_engine
):
    """The board goes (another device deleted it) between the membership check and the insert:
    the foreign key refuses the row, which is a 404 like any other vanished board."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from easytodo.content import service
    from easytodo.models import Board

    check = service.require_board

    async def check_then_delete(session, board_id, user, action):
        found = await check(session, board_id, user, action)
        async with AsyncSession(_db_engine) as other:
            await other.execute(delete(Board).where(Board.id == board_id))
            await other.commit()
        return found

    monkeypatch.setattr(service, "require_board", check_then_delete)

    r = await editor_client.post(
        f"/boards/{board.id}/categories", data={"name": "Bakery"}, headers=HX
    )

    assert r.status_code == 404
    assert toast(r) == "Board not found."


async def test_reorder_locks_the_categories_in_id_order(editor_client, board, db, monkeypatch):
    """Two reorders at once take the row locks in the same order, so they cannot deadlock."""
    from easytodo.content import service

    first = await create_category(db, board, "Dairy", 0)
    second = await create_category(db, board, "Bakery", 1)
    statements: list[str] = []
    execute = service.AsyncSession.scalars

    async def spy(self, statement, *args, **kwargs):
        statements.append(str(statement))
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(service.AsyncSession, "scalars", spy)

    r = await editor_client.put(
        f"/boards/{board.id}/categories/order", data={"ids": [second.id, first.id]}, headers=HX
    )

    assert r.status_code == 204
    [locking] = [s for s in statements if "FOR UPDATE" in s]
    assert "ORDER BY categories.id" in locking
