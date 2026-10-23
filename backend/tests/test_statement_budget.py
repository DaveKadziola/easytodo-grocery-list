"""How many SQL statements the hot requests run, and the two changes that keep that low: a category
read in two statements (it with the viewer's sort row, then its items), and the item row as a
macro (the markup must not change with it).

Each extra statement on a write costs 7-10 % (perf-investigation.md). Raise a budget only with a
bench."""

import re
from types import SimpleNamespace

import pytest
from boards.test_item_views import AVATAR_AFTER_THE_NAME, add_member, attach
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, count_queries, signed_in

from easytodo.boards.service import SortChoice
from easytodo.content.service import get_category_view
from easytodo.models import BoardRole, CategorySortPref

HX = {"HX-Request": "true"}


def statements_of(app):
    """``count_queries`` for the statements the running ``app`` runs (its own engine, not the
    test session's). The budgets below are counted with no ``last_seen_at`` bump due (the session
    is new): a request that bumps it runs one more statement, once in 5 minutes per session."""
    return count_queries(SimpleNamespace(bind=app.state.engine))


@pytest.fixture
async def shared(db):
    """A board of an owner and an editor with two categories, an item of the owner's in each, and
    the editor's own sort row for the second."""
    owner = await create_user(db, "owner@example.com")
    editor = await create_user(db, "editor@example.com")
    board = await create_board(db, owner, "Groceries", {editor: BoardRole.EDITOR})
    dairy = await create_category(db, board, "Dairy", position=0)
    milk = await create_item(db, dairy, "Milk", created_by=owner)
    fruit = await create_category(db, board, "Fruit", position=1)
    await create_item(db, fruit, "Apple", created_by=owner)
    db.add(
        CategorySortPref(
            user_id=editor.id, category_id=fruit.id, field="name", descending=True, open_first=True
        )
    )
    await db.commit()
    return SimpleNamespace(owner=owner, editor=editor, board=board, dairy=dairy, milk=milk)


async def test_category_fragment_statement_budget(app, client_factory, shared):
    editor = await signed_in(client_factory, "editor@example.com")

    with statements_of(app) as added:
        r = await editor.post(
            f"/categories/{shared.dairy.id}/items", data={"name": "Butter"}, headers=HX
        )
    assert r.status_code == 200
    with statements_of(app) as toggled:
        r = await editor.post(f"/items/{shared.milk.id}/toggle", headers=HX)
    assert r.status_code == 200

    # Each extra statement on a write costs 7-10 % (perf-investigation.md). Raise this only with
    # a bench.
    assert len(added) <= 6, added
    assert len(toggled) <= 6, toggled


async def test_board_page_statement_budget(app, client_factory, shared):
    editor = await signed_in(client_factory, "editor@example.com")

    with statements_of(app) as page:
        r = await editor.get(f"/boards/{shared.board.id}")
    assert r.status_code == 200
    assert "Milk" in r.text
    with statements_of(app) as content:  # the fragment the page fetches again over its socket
        r = await editor.get(f"/boards/{shared.board.id}/content")
    assert r.status_code == 200

    # The page is the request people wait for most. Raise this only with a bench.
    assert len(page) <= 5, page
    assert len(content) <= 4, content


async def test_category_view_takes_two_statements(db):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner, "Groceries")
    fruit = await create_category(db, board, "Fruit", position=0)
    for name, done in (("apple", False), ("Banana", True), ("Cherry", False), ("Almond", False)):
        await create_item(db, fruit, name, is_done=done)
    empty = await create_category(db, board, "Empty", position=1)  # no item to carry the sort row
    plain = await create_category(db, board, "Plain", position=2)  # no sort row at all
    for category in (fruit, empty):
        db.add(
            CategorySortPref(
                user_id=owner.id,
                category_id=category.id,
                field="name",
                descending=True,
                open_first=True,
            )
        )
    await db.commit()
    db.expunge_all()

    with count_queries(db) as statements:
        view, role = await get_category_view(db, fruit.id, owner)
    assert len(statements) == 2
    assert role == BoardRole.OWNER
    assert view.sort == SortChoice("name", True, True, custom=True)
    # Open items first, each part Z-A ignoring case.
    assert [iv.item.name for iv in view.items] == ["Cherry", "apple", "Almond", "Banana"]

    with count_queries(db) as statements:
        view, _ = await get_category_view(db, empty.id, owner)
    assert len(statements) == 2
    assert (view.items, view.sort) == ([], SortChoice("name", True, True, custom=True))

    with count_queries(db) as statements:
        view, _ = await get_category_view(db, plain.id, owner)
    assert len(statements) == 2
    assert not view.sort.custom


def row_of(html: str, item) -> str:
    """The ``<li>`` of ``item`` in ``html``."""
    found = re.search(rf'<li id="item-{item.id}".*?</li>', html, re.DOTALL)
    assert found, item.name
    return found[0]


async def test_item_row_macro_renders_like_before(client_factory, db, shared):
    board, dairy, milk = shared.board, shared.dairy, shared.milk
    for position, done in enumerate((True, False, True)):
        await create_checklist_entry(db, milk, f"Step {position}", is_done=done, position=position)
    attach(db, milk, 1)
    await db.commit()
    bread = await create_item(
        db, dairy, "Bread", is_done=True, created_by=shared.editor, description="Wholemeal"
    )
    nasty = await create_item(db, dairy, NASTY, description=NASTY)  # nobody's: no avatar
    viewer = await create_user(db, "viewer@example.com")
    await add_member(db, board, viewer, BoardRole.VIEWER)
    editor_client = await signed_in(client_factory, "editor@example.com")
    viewer_client = await signed_in(client_factory, "viewer@example.com")

    html = (await editor_client.get(f"/categories/{dairy.id}")).text

    assert sorted(re.findall(r'<li id="item-(\d+)"', html)) == sorted(
        str(item.id) for item in (milk, bread, nasty)
    )
    milk_row = row_of(html, milk)
    assert milk_row.startswith(
        f'<li id="item-{milk.id}" data-item-id="{milk.id}" class="flex items-start">'
    )
    assert (
        f'<input type="checkbox" id="item-{milk.id}-done" class="size-5 accent-emerald-600" '
        f'aria-labelledby="item-{milk.id}-name" hx-post="/items/{milk.id}/toggle" '
        f'hx-target="#category-{dairy.id}">'
    ) in milk_row
    assert (
        f'<button type="button" id="item-{milk.id}-name" class="item-name" data-name="Milk" '
        'data-description="" @click="openItem">Milk</button>'
    ) in milk_row
    assert AVATAR_AFTER_THE_NAME.search(milk_row)
    bread_row = row_of(html, bread)
    assert 'class="flex items-start text-slate-500 dark:text-slate-400 line-through"' in bread_row
    assert f'hx-target="#category-{dairy.id}" checked>' in bread_row
    assert (
        '<p class="-mt-1.5 whitespace-pre-line break-words pb-2 text-sm text-slate-600 '
        'dark:text-slate-400">Wholemeal</p>'
    ) in bread_row
    assert 'aria-hidden="true" title="editor">E</span>' in bread_row
    nasty_row = row_of(html, nasty)
    assert f'data-name="{NASTY_ESCAPED}" data-description="{NASTY_ESCAPED}"' in nasty_row
    assert NASTY not in html
    assert 'class="avatar' not in nasty_row

    # A viewer gets the same rows without the checkbox: the name opens the read-only dialog.
    viewer_html = (await viewer_client.get(f"/categories/{dairy.id}")).text
    viewer_milk = row_of(viewer_html, milk)
    assert (
        f'<button type="button" id="item-{milk.id}-name" class="item-name" data-name="Milk" '
        'data-description="" @click="openItem">Milk</button>'
    ) in viewer_milk
    assert 'aria-hidden="true" title="owner">O</span>' in viewer_milk
    assert 'type="checkbox"' not in viewer_html
    assert row_of(viewer_html, nasty).count('data-name="' + NASTY_ESCAPED + '"') == 1
