"""Board content is reachable only through the user's own board memberships (Review Focus 4)."""

import json

import pytest
from factories import create_board, create_category, create_item, create_user
from sqlalchemy import select

from easytodo.content.routes import router
from easytodo.models import BoardRole, Item

HX = {"HX-Request": "true"}
NO_PERMISSION = "You don't have permission to do that."

# Every content route, as (method, path template, form data); the ids are filled in per test.
ROUTES = [
    ("GET", "/boards/{board}/content", None),
    ("POST", "/boards/{board}/categories", {"name": "Hijacked"}),
    ("PUT", "/boards/{board}/categories/order", {"ids": ["{category}", "{other_category}"]}),
    ("GET", "/categories/{category}", None),
    ("PATCH", "/categories/{category}", {"name": "Hijacked"}),
    ("DELETE", "/categories/{category}", None),
    ("POST", "/categories/{category}/items", {"name": "Hijacked", "description": "x"}),
    ("PATCH", "/items/{item}", {"name": "Hijacked", "description": "x"}),
    ("POST", "/items/{item}/toggle", None),
    ("POST", "/items/{item}/move", {"category_id": "{other_category}"}),
    ("DELETE", "/items/{item}", None),
    ("POST", "/categories/{category}/clear-done", None),
]
WRITES = [route for route in ROUTES if route[0] != "GET"]


def fill(route, **ids):
    method, path, data = route
    if data is not None:
        data = {
            key: [v.format(**ids) for v in value]
            if isinstance(value, list)
            else value.format(**ids)
            for key, value in data.items()
        }
    return method, path.format(**ids), data


def test_routes_cover_every_content_route():
    registered = {
        (method, route.path.replace("_id}", "}"))
        for route in router.routes
        for method in route.methods
    }
    assert registered == {(method, path) for method, path, _ in ROUTES}


async def test_viewer_can_read_but_not_modify(viewer_client, board, item, db, content_snapshot):
    other = await create_category(db, board, "Bakery", position=1)
    await create_item(db, other, "Old bread", is_done=True)
    before = await content_snapshot()
    ids = {"board": board.id, "category": item.category_id, "item": item.id}

    for path in ("/boards/{board}", "/boards/{board}/content", "/categories/{category}"):
        r = await viewer_client.get(path.format(**ids))
        assert r.status_code == 200, path
        assert f'id="item-{item.id}"' in r.text and "Milk" in r.text

    for route in WRITES:
        method, path, data = fill(route, **ids, other_category=other.id)
        r = await viewer_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 403, (method, path)
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": NO_PERMISSION}
        }

    assert await content_snapshot() == before


@pytest.mark.parametrize("bad_id", ["999999", "0", "-1", str(2**63)])
async def test_unknown_or_impossible_ids_are_404(editor_client, item, db, content_snapshot, bad_id):
    before = await content_snapshot()
    ids = dict.fromkeys(("board", "category", "other_category", "item"), bad_id)
    requests = [fill(route, **ids) for route in ROUTES] + [
        ("POST", f"/items/{item.id}/move", {"category_id": bad_id}),
    ]
    for method, path, data in requests:
        r = await editor_client.request(method, path, data=data, headers=HX)
        assert r.status_code == 404, (method, path)
        assert json.loads(r.headers["hx-trigger"])["resync"] is True
    assert await content_snapshot() == before


async def test_other_users_ids_are_404_everywhere(
    client_factory, db, signed_in_client, content_snapshot
):  # Review Focus 4
    alice = await create_user(db, "alice@example.com")
    bob = await create_user(db, "bob@example.com")
    board = await create_board(db, alice, "Alice's groceries")
    dairy = await create_category(db, board, "Dairy", position=0)
    fruit = await create_category(db, board, "Fruit", position=1)
    milk = await create_item(db, dairy, "Milk")
    await create_item(db, dairy, "Old cheese", is_done=True)
    bobs_board = await create_board(db, bob, "Bob's list")
    bobs_category = await create_category(db, bobs_board, "Bob's things")
    bobs_item = await create_item(db, bobs_category, "Bob's item")
    before = await content_snapshot()
    client = await signed_in_client("bob@example.com")

    alices_ids = {
        "board": board.id,
        "category": dairy.id,
        "other_category": fruit.id,
        "item": milk.id,
    }
    requests = [fill(route, **alices_ids) for route in ROUTES] + [
        ("GET", f"/boards/{board.id}", None),
        # Bob's own item into Alice's category, and Alice's item into Bob's category.
        ("POST", f"/items/{bobs_item.id}/move", {"category_id": str(dairy.id)}),
        ("POST", f"/items/{milk.id}/move", {"category_id": str(bobs_category.id)}),
    ]
    for method, path, data in requests:
        for headers in ({}, HX):
            r = await client.request(method, path, data=data, headers=headers)
            assert r.status_code == 404, (method, path, headers)
            for secret in ("Alice", "Dairy", "Fruit", "Milk", "cheese"):
                assert secret not in r.text, (method, path, secret)
            if headers:
                assert json.loads(r.headers["hx-trigger"])["resync"] is True

    # Bob's own board does not take Alice's categories into its order either.
    r = await client.put(
        f"/boards/{bobs_board.id}/categories/order",
        data={"ids": [str(bobs_category.id), str(dairy.id)]},
        headers=HX,
    )
    assert r.status_code == 422
    assert await content_snapshot() == before


async def test_move_item_to_category_in_other_board_is_404(
    client_factory, db, signed_in_client, content_snapshot
):  # Review Focus 4
    alice = await create_user(db, "alice@example.com")
    eve = await create_user(db, "eve@example.com")
    x = await create_board(db, alice, "X")
    y = await create_board(db, eve, "Y", members={alice: BoardRole.EDITOR})
    x_category = await create_category(db, x, "Dairy")
    y_category = await create_category(db, y, "Tools")
    milk = await create_item(db, x_category, "Milk")
    hammer = await create_item(db, y_category, "Hammer")
    before = await content_snapshot()
    client = await signed_in_client("alice@example.com")

    for item, target in ((milk, y_category), (hammer, x_category)):
        r = await client.post(f"/items/{item.id}/move", data={"category_id": target.id}, headers=HX)
        assert r.status_code == 404
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": "This category no longer exists."},
            "resync": True,
        }

    rows = (await db.execute(select(Item.id, Item.category_id, Item.board_id))).all()
    assert set(rows) == {(milk.id, x_category.id, x.id), (hammer.id, y_category.id, y.id)}
    assert await content_snapshot() == before
