"""Archiving a category (editors and owners): it leaves the board for every member, its items kept,
until it is restored from the board's "Archived categories" page (last on the board, renamed when
its name was taken meanwhile) or deleted there for good by typing its name."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from factories import create_board, create_category, create_item, create_user
from helpers import NASTY, NASTY_ESCAPED, TYPED_NAMES, flash_of, text_of, toast_message
from sqlalchemy import select, update

from content.test_board_markup import section
from content.test_isolation import ROUTES, fill
from easytodo.boards.service import list_boards
from easytodo.content import service
from easytodo.content.service import CATEGORY_GONE, CATEGORY_NOT_ARCHIVED, CONFIRM_CATEGORY_NAME
from easytodo.errors import Conflict
from easytodo.models import Category, Item, User
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_HEADER

HX = {"HX-Request": "true"}
NO_PERMISSION = "You don't have permission to do that."
DUPLICATE = "A category with this name already exists."
ARCHIVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# The only content routes that reach an archived category.
ARCHIVE_ROUTES = {("POST", "/categories/{category}/restore"), ("DELETE", "/categories/{category}")}


async def archive(db, category: Category) -> None:
    """Archive ``category`` straight in the database (no event)."""
    await db.execute(
        update(Category).where(Category.id == category.id).values(archived_at=ARCHIVED_AT)
    )
    await db.commit()


async def board_categories(client, board_id: int) -> list[str]:
    """The names of the categories on the board's page, in their order there."""
    html = (await client.get(f"/boards/{board_id}/content")).text
    return re.findall(r"<h2[^>]*>([^<]*)</h2>", html)


def archived_entry(html: str, category_id: int) -> str:
    match = re.search(rf'<li id="archived-category-{category_id}".*?</li>', html, re.DOTALL)
    assert match, f"no archived category {category_id} on the page"
    return match[0]


async def test_archive_leaves_the_rows_but_takes_the_category_off_the_board(
    editor_client, board, category, item, db, published
):
    bakery = await create_category(db, board, "Bakery", position=1)
    await create_item(db, bakery, "Bread", is_done=True)

    r = await editor_client.post(
        f"/categories/{category.id}/archive", headers=HX | {"X-Client-Id": "tab-1"}
    )

    assert r.status_code == 200
    assert r.content == b""  # HTMX swaps the category out
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "info", "message": "Category archived."}
    }
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
    row = (
        await db.execute(
            select(Category.name, Category.position, Category.archived_at).where(
                Category.id == category.id
            )
        )
    ).one()
    assert (row.name, row.position) == ("Dairy", 0)
    assert abs(datetime.now(UTC) - row.archived_at) < timedelta(minutes=1)
    assert list(await db.scalars(select(Item.name).order_by(Item.id))) == ["Milk", "Bread"]
    for path in (f"/boards/{board.id}", f"/boards/{board.id}/content"):
        html = (await editor_client.get(path)).text
        assert f'id="category-{bakery.id}"' in html, path
        assert f'id="category-{category.id}"' not in html, path
        assert "Dairy" not in html and "Milk" not in html, path  # the item modal's list too
    r = await editor_client.get(f"/categories/{category.id}", headers=HX)
    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": CATEGORY_GONE},
        "resync": True,
    }
    [summary] = await list_boards(db, await db.get_one(User, board.owner_id))
    assert summary.open_items == 0  # Milk is open, but in the archive


async def test_reorder_move_and_new_category_ignore_archived_ones(editor_client, board, db):
    bakery = await create_category(db, board, "Bakery", position=1)
    fruit = await create_category(db, board, "Fruit", position=0)
    dairy = await create_category(db, board, "Dairy", position=9, archived_at=ARCHIVED_AT)
    apple = await create_item(db, fruit, "Apple")
    order = f"/boards/{board.id}/categories/order"

    r = await editor_client.put(order, data={"ids": [bakery.id, fruit.id, dairy.id]}, headers=HX)
    assert r.status_code == 422
    r = await editor_client.put(order, data={"ids": [bakery.id, fruit.id]}, headers=HX)
    assert r.status_code == 204
    r = await editor_client.post(
        f"/items/{apple.id}/move", data={"category_id": dairy.id}, headers=HX
    )
    assert r.status_code == 404
    assert toast_message(r) == CATEGORY_GONE
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": "Dairy"})
    assert r.status_code == 201  # an archived category leaves its name free

    rows = await db.execute(
        select(Category.name, Category.position, Category.archived_at.is_not(None)).order_by(
            Category.id
        )
    )
    assert rows.all() == [
        ("Bakery", 0, False),
        ("Fruit", 1, False),
        ("Dairy", 9, True),
        ("Dairy", 2, False),  # after the active ones
    ]
    assert await db.scalar(select(Item.category_id).where(Item.id == apple.id)) == fruit.id


async def test_archived_category_is_404_everywhere(  # Review Focus 1
    editor_client, signed_in_client, board, db, content_snapshot
):
    dairy = await create_category(db, board, "Dairy", position=0)
    bakery = await create_category(db, board, "Bakery", position=1)
    milk = await create_item(db, dairy, "Milk")
    await create_item(db, bakery, "Bread")
    owner_client = await signed_in_client("owner@example.com")
    r = await editor_client.post(f"/categories/{dairy.id}/archive", headers=HX)
    assert r.status_code == 200
    before = await content_snapshot()

    # Every id, though the /boards/ routes are left out: route data may name any of them (the
    # board, for moving a category to another one).
    ids = {"board": board.id, "category": dairy.id, "other_category": bakery.id, "item": milk.id}
    requests = [
        fill(route, **ids)
        for route in ROUTES
        if not route[1].startswith("/boards/") and route[:2] not in ARCHIVE_ROUTES
    ]
    assert len(requests) >= 9
    for client in (owner_client, editor_client):
        for method, path, data in requests:
            r = await client.request(method, path, data=data, headers=HX)
            assert r.status_code == 404, (method, path)
            assert json.loads(r.headers["hx-trigger"])["resync"] is True, (method, path)

    assert await content_snapshot() == before


async def test_restore_and_delete_need_an_editor(
    viewer_client, signed_in_client, board, db, content_snapshot
):
    dairy = await create_category(db, board, "Dairy", archived_at=ARCHIVED_AT)
    await create_item(db, dairy, "Milk")
    await create_user(db, "stranger@example.com")
    stranger = await signed_in_client("stranger@example.com")
    before = await content_snapshot()
    prompt = {"HX-Prompt": "Dairy"}

    for method, path, status in (
        ("POST", f"/categories/{dairy.id}/restore", 403),
        ("DELETE", f"/categories/{dairy.id}", 403),
        ("POST", f"/categories/{dairy.id}/archive", 404),  # only an active one is archived
    ):
        r = await viewer_client.request(method, path, headers=HX | prompt)
        assert r.status_code == status, (method, path)
        if status == 403:
            assert toast_message(r) == NO_PERMISSION
    for method, path in (
        ("POST", f"/categories/{dairy.id}/restore"),
        ("DELETE", f"/categories/{dairy.id}"),
        ("GET", f"/boards/{board.id}/archive"),
    ):
        for headers in (prompt, HX | prompt):
            r = await stranger.request(method, path, headers=headers)
            assert r.status_code == 404, (method, path, headers)
            for secret in ("Groceries", "Dairy", "Milk"):
                assert secret not in r.text, (method, path, secret)

    assert await content_snapshot() == before


async def test_restore_puts_it_last_with_its_items(editor_client, board, db, published):
    # Its old position is past the active ones': that does not count.
    dairy = await create_category(db, board, "Dairy", position=9, archived_at=ARCHIVED_AT)
    milk = await create_item(db, dairy, "Milk")
    await create_category(db, board, "Bakery", position=3)
    await create_category(db, board, "Fruit", position=4)

    r = await editor_client.post(
        f"/categories/{dairy.id}/restore", headers=HX | {"X-Client-Id": "tab-1"}
    )

    assert r.status_code == 200
    assert r.headers["hx-refresh"] == "true"  # the archive page, without it
    assert flash_of(r) == {"level": "info", "message": "Category restored."}
    assert published == [
        (
            (BoardEvent(EventType.CATEGORY_CHANGED, board_id=board.id, category_ids=(dairy.id,)),),
            "tab-1",
        )
    ]
    row = (
        await db.execute(
            select(Category.name, Category.position, Category.archived_at).where(
                Category.id == dairy.id
            )
        )
    ).one()
    assert tuple(row) == ("Dairy", 5, None)
    assert await board_categories(editor_client, board.id) == ["Bakery", "Fruit", "Dairy"]
    html = (await editor_client.get(f"/boards/{board.id}/content")).text
    assert f'id="item-{milk.id}"' in section(html, dairy.id)

    r = await editor_client.post(f"/categories/{dairy.id}/restore", headers=HX)
    assert r.status_code == 404  # only an archived category is restored
    assert toast_message(r) == CATEGORY_GONE


async def test_restore_with_a_taken_name_adds_a_number(editor_client, board, db):
    await create_category(db, board, "dairy", position=0)
    first = await create_category(db, board, "Dairy", archived_at=ARCHIVED_AT)
    second = await create_category(db, board, "Dairy", archived_at=ARCHIVED_AT)

    for category, name in ((first, "Dairy (2)"), (second, "Dairy (3)")):
        r = await editor_client.post(f"/categories/{category.id}/restore", headers=HX)
        assert r.status_code == 200
        assert flash_of(r) == {"level": "info", "message": f"Category restored as “{name}”."}

    assert await board_categories(editor_client, board.id) == ["dairy", "Dairy (2)", "Dairy (3)"]


async def test_suggest_category_name(db):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner)
    other = await create_board(db, owner, "Other")
    await create_category(db, board, "Dairy", position=0)
    await create_category(db, board, "dairy (2)", position=1)
    # Neither the archive nor another board counts.
    await create_category(db, board, "Dairy (3)", position=2, archived_at=ARCHIVED_AT)
    await create_category(db, other, "Dairy (3)")
    long_name = "x" * 120
    await create_category(db, board, long_name, position=3)
    await create_category(db, board, "x" * 115 + " yyyy", position=4)

    suggest = service.suggest_category_name
    assert await suggest(db, board.id, "Bakery") == "Bakery"
    assert await suggest(db, board.id, "DAIRY") == "DAIRY (3)"  # ignoring case on both sides
    assert await suggest(db, other.id, "Dairy") == "Dairy"
    assert await suggest(db, board.id, long_name) == "x" * 116 + " (2)"  # 120 characters at most
    assert await suggest(db, board.id, "x" * 115 + " yyyy") == "x" * 115 + " (2)"

    db.add_all(
        Category(board_id=board.id, name=f"Dairy ({n})", position=n + 2) for n in range(3, 101)
    )
    await db.commit()
    with pytest.raises(Conflict) as raised:
        await suggest(db, board.id, "Dairy")
    assert raised.value.message == DUPLICATE
    assert service._unique_category_name.__doc__.startswith(
        "Write the new, renamed or restored category inside the block"
    )  # triage #57


async def test_permanent_delete_needs_an_archived_category_and_its_exact_name(
    editor_client, board, db, published, content_snapshot
):
    dairy = await create_category(db, board, "Dairy", position=0)
    await create_item(db, dairy, "Milk")
    await create_item(db, await create_category(db, board, "Bakery", position=1), "Bread")

    r = await editor_client.delete(f"/categories/{dairy.id}", headers=HX | {"HX-Prompt": "Dairy"})
    assert r.status_code == 409
    assert toast_message(r) == CATEGORY_NOT_ARCHIVED

    await archive(db, dairy)
    before = await content_snapshot()
    for prompt in ({}, {"HX-Prompt": ""}, {"HX-Prompt": "dairy"}, {"HX-Prompt": "Dairy!"}):
        r = await editor_client.delete(f"/categories/{dairy.id}", headers=HX | prompt)
        assert r.status_code == 422, prompt
        assert toast_message(r) == CONFIRM_CATEGORY_NAME
    assert await content_snapshot() == before
    published.clear()

    r = await editor_client.delete(f"/categories/{dairy.id}", headers=HX | {"HX-Prompt": " Dairy "})

    assert r.status_code == 200
    assert r.headers["hx-refresh"] == "true"
    assert flash_of(r) == {"level": "info", "message": "Category deleted permanently."}
    assert list(await db.scalars(select(Category.name))) == ["Bakery"]
    assert list(await db.scalars(select(Item.name))) == ["Bread"]
    assert published == [((), None)]  # nobody has an archived category open


@pytest.mark.parametrize(("name", "prompt"), TYPED_NAMES)  # Review Focus 3
async def test_permanent_delete_accepts_a_non_latin1_name(editor_client, board, db, name, prompt):
    doomed = await create_category(db, board, name, archived_at=ARCHIVED_AT)
    await create_item(db, doomed, "Milk")
    await create_item(db, await create_category(db, board, "Bakery"), "Bread")

    r = await editor_client.delete(f"/categories/{doomed.id}", headers=HX | prompt)

    assert r.status_code == 200, r.headers.get("hx-trigger")
    assert r.headers["hx-refresh"] == "true"
    assert flash_of(r) == {"level": "info", "message": "Category deleted permanently."}
    assert list(await db.scalars(select(Category.name))) == ["Bakery"]
    assert list(await db.scalars(select(Item.name))) == ["Bread"]


async def test_archive_page_lists_archived_categories(editor_client, viewer_client, board, db):
    newer = await create_category(db, board, NASTY, position=0, archived_at=ARCHIVED_AT)
    for name in ("Milk", "Butter", "Cheese"):
        await create_item(db, newer, name)
    await db.execute(update(Item).where(Item.name == "Cheese").values(deleted_at=ARCHIVED_AT))
    older = await create_category(
        db, board, "Old dairy", position=1, archived_at=datetime(2026, 9, 2, tzinfo=UTC)
    )
    await create_item(db, older, "Cream", is_done=True)
    await create_category(db, board, "Bakery", position=2)  # active
    stranger = await create_user(db, "stranger@example.com")
    elsewhere = await create_board(db, stranger, "Elsewhere")
    await create_category(db, elsewhere, "Not here", archived_at=ARCHIVED_AT)
    # The board owner's retention (30 days) decides, not the editor's.
    await db.execute(
        update(User).where(User.email == "editor@example.com").values(archive_retention_days=7)
    )
    await db.commit()

    html = (await editor_client.get(f"/boards/{board.id}/archive")).text

    assert "<title>Archived categories · Groceries · EasyToDo</title>" in html
    assert re.search(r"<h1[^>]*>Archived categories in Groceries</h1>", html)
    assert re.search(rf'<a href="/boards/{board.id}"[^>]*>Back to the board</a>', html)
    assert re.findall(r'<li id="archived-category-(\d+)"', html) == [str(newer.id), str(older.id)]
    entry = archived_entry(html, newer.id)
    assert re.search(rf"<h2[^>]*>{re.escape(NASTY_ESCAPED)}</h2>", entry)
    assert "2 items · Archived 2026-10-01 12:00 UTC" in text_of(entry)  # not the deleted one
    assert '<time datetime="2026-10-01T12:00:00+00:00"' in entry
    assert '<time datetime="2026-10-31T12:00:00+00:00"' in entry
    assert "Deleted permanently on 2026-10-31 12:00 UTC" in text_of(entry)
    assert re.search(
        rf'<button [^>]*class="btn-secondary"[^>]*hx-post="/categories/{newer.id}/restore"'
        r"[^>]*>Restore</button>",
        entry,
    )
    assert re.search(
        rf'<button [^>]*class="btn-danger"[^>]*hx-delete="/categories/{newer.id}"', entry
    )
    assert (
        f'hx-prompt="Type “{NASTY_ESCAPED}” to delete this category and its items permanently."'
    ) in entry
    entry = archived_entry(html, older.id)
    assert "1 item · Archived 2026-09-02 00:00 UTC" in text_of(entry)
    assert "Deleted permanently on 2026-10-02 00:00 UTC" in text_of(entry)
    assert "<b>" not in html and "<script>alert" not in html
    assert "Bakery" not in html and "Not here" not in html
    # Nothing that board.js or realtime.js would take for a category on the board.
    assert "data-category-" not in html and 'id="category-' not in html

    viewer_html = (await viewer_client.get(f"/boards/{board.id}/archive")).text
    assert re.findall(r'<li id="archived-category-(\d+)"', viewer_html) == [
        str(newer.id),
        str(older.id),
    ]
    assert "hx-post" not in viewer_html and "hx-delete" not in viewer_html
    assert "Restore" not in viewer_html and "Delete permanently</button>" not in viewer_html
    archive_link = (
        rf'<a href="/boards/{board.id}/archive" class="link inline-flex min-h-11 items-center'
        r' text-sm">Archived categories</a>'
    )
    for client in (editor_client, viewer_client):
        for path in (f"/boards/{board.id}", f"/boards/{board.id}/content"):
            assert re.search(archive_link, (await client.get(path)).text), path

    await db.execute(
        update(User).where(User.id == board.owner_id).values(archive_retention_days=None)
    )
    await db.commit()
    html = (await editor_client.get(f"/boards/{board.id}/archive")).text
    assert "Kept until you delete it" in text_of(archived_entry(html, newer.id))
    assert "Deleted permanently on" not in html

    await db.execute(update(Category).where(Category.board_id == board.id).values(archived_at=None))
    await db.commit()
    html = (await editor_client.get(f"/boards/{board.id}/archive")).text
    assert "No archived categories." in html and "archived-category-" not in html


@pytest.mark.parametrize("breach", ["no_token", "foreign_origin"])
async def test_archive_writes_require_the_csrf_token(
    editor_client, board, db, content_snapshot, breach
):
    active = await create_category(db, board, "Dairy")
    archived = await create_category(db, board, "Bakery", position=1, archived_at=ARCHIVED_AT)
    before = await content_snapshot()
    if breach == "no_token":
        del editor_client.headers[CSRF_HEADER]
    else:
        editor_client.headers["Origin"] = "https://evil.example"

    for method, path in (
        ("POST", f"/categories/{active.id}/archive"),
        ("POST", f"/categories/{archived.id}/restore"),
        ("DELETE", f"/categories/{archived.id}"),
    ):
        r = await editor_client.request(method, path, headers=HX | {"HX-Prompt": "Bakery"})
        assert r.status_code == 403, (method, path)

    assert await content_snapshot() == before
