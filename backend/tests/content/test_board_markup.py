"""The board's HTML: edit controls only for members who may edit, fragments that match the full
page, item data passed to the modal only through escaped data-* attributes, templates that keep
to the CSP (no inline scripts, handlers or styles), empty states, the "Clear done (n)" count, the
connection status in the header and the phone's bottom bar."""

import re

from factories import create_board, create_category, create_item, create_user
from helpers import NASTY, NASTY_ESCAPED
from sqlalchemy import select, update

from easytodo.models import Item, User
from easytodo.web import FRONTEND_DIR

HX = {"HX-Request": "true"}
# Markup that only someone who may change the board's content gets.
EDIT_CONTROLS = (
    'hx-post="/items/',
    "hx-patch=",
    "hx-delete=",
    "hx-put=",
    'type="checkbox"',
    "data-sortable",
    "category-handle",
    "Add item",
    "New category",
    'x-data="itemModal"',
    'name="description"',
)


def section(html: str, category_id: int) -> str:
    """The ``<section>`` of one category in ``html``."""
    match = re.search(rf'<section id="category-{category_id}".*?</section>', html, re.DOTALL)
    assert match, f"no category {category_id} in the page"
    return match[0]


def board_content(html: str) -> str:
    """Everything from ``#board-content`` on (the header and the categories)."""
    return html[html.index('<div id="board-content"') :]


def test_templates_have_no_inline_handlers():  # Review Focus 1 + CSP
    paths = sorted((FRONTEND_DIR / "templates").rglob("*.html"))
    assert paths
    for path in paths:
        text = path.read_text()
        assert not re.search(r"\son[a-z]+\s*=", text), path
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path
        # The CSP has no 'unsafe-eval' or 'unsafe-inline' for these either.
        assert "hx-on" not in text, path
        assert not re.search(r"\sstyle\s*=|<style", text), path


async def test_viewer_sees_no_edit_controls(viewer_client, board, item):
    html = (await viewer_client.get(f"/boards/{board.id}")).text
    assert 'hx-post="/items/' not in html and "Add item" not in html
    assert "Milk" in html
    content = board_content(html)
    for control in EDIT_CONTROLS:
        assert control not in content, control


async def test_viewer_category_fragment_has_no_edit_controls(viewer_client, board, item):
    for path in (f"/categories/{item.category_id}", f"/boards/{board.id}/content"):
        html = (await viewer_client.get(path, headers=HX)).text
        assert 'hx-post="/items/' not in html and "Add item" not in html
        assert "Milk" in html
        for control in EDIT_CONTROLS:
            assert control not in html, (path, control)


async def test_editor_category_fragment_has_edit_controls(editor_client, category, item):
    html = (await editor_client.get(f"/categories/{category.id}", headers=HX)).text
    assert "Add item" in html
    assert re.search(
        rf'<form hx-post="/categories/{category.id}/items" hx-target="#category-{category.id}"'
        rf' data-refocus="new-item-{category.id}"',
        html,
    )
    assert re.search(
        rf'<input id="new-item-{category.id}" name="name" placeholder="Add item"', html
    )
    assert re.search(
        rf'<input type="checkbox"[^>]* hx-post="/items/{item.id}/toggle"'
        rf' hx-target="#category-{category.id}"',
        html,
    )
    assert f'hx-patch="/categories/{category.id}"' in html  # rename
    assert re.search(
        rf'hx-delete="/categories/{category.id}" hx-target="#category-{category.id}"'
        r' hx-confirm="[^"]+"',
        html,
    )
    assert "category-handle" in html and "data-sortable-items" in html
    assert re.search(r'<button type="button" id="item-\d+-name"[^>]* @click="openItem"', html)


async def test_every_category_response_keeps_the_editor_controls(
    editor_client, board, category, item
):
    responses = [
        await editor_client.post(f"/boards/{board.id}/categories", data={"name": "Bakery"}),
        await editor_client.patch(f"/categories/{category.id}", data={"name": "Fridge"}),
        await editor_client.post(f"/categories/{category.id}/items", data={"name": "Butter"}),
        await editor_client.post(f"/items/{item.id}/toggle"),
        await editor_client.patch(f"/items/{item.id}", data={"name": "Oat milk"}),
        await editor_client.post(f"/categories/{category.id}/clear-done"),
    ]
    for r in responses:
        assert r.status_code in (200, 201), r.text
        assert "Add item" in r.text and "category-handle" in r.text, r.request.url


async def test_fragment_and_full_page_render_same_category(editor_client, category, item):
    fragment = section((await editor_client.get(f"/categories/{category.id}")).text, category.id)
    for path in (f"/boards/{category.board_id}", f"/boards/{category.board_id}/content"):
        assert section((await editor_client.get(path)).text, category.id) == fragment
    assert "Add item" in fragment and "Milk" in fragment


async def test_board_content_carries_the_board_header(signed_in_client, editor_client, board):
    owner_client = await signed_in_client("owner@example.com")
    owner_html = (await owner_client.get(f"/boards/{board.id}/content", headers=HX)).text
    assert f'data-board-id="{board.id}" data-board-name="Groceries"' in owner_html
    assert re.search(r"<h1[^>]*>Groceries</h1>", owner_html)
    assert '<a href="/boards"' in owner_html
    assert f'hx-patch="/boards/{board.id}"' in owner_html
    assert f'hx-post="/boards/{board.id}/archive"' in owner_html

    editor_html = (await editor_client.get(f"/boards/{board.id}/content", headers=HX)).text
    assert re.search(r"<h1[^>]*>Groceries</h1>", editor_html)
    assert f'hx-patch="/boards/{board.id}"' not in editor_html
    assert f'hx-post="/boards/{board.id}/archive"' not in editor_html
    assert f'hx-post="/boards/{board.id}/categories"' in editor_html  # New category
    assert re.search(r'<div id="categories"[^>]* data-sortable-categories>', editor_html)


async def test_item_modal_edit_form_always_sends_the_description(editor_client, board, item, db):
    await db.execute(update(Item).where(Item.id == item.id).values(description="2 litres"))
    await db.commit()

    html = (await editor_client.get(f"/boards/{board.id}")).text

    assert '<div x-data="itemModal">' in html
    form = re.search(r'<form x-ref="form".*?</form>', html, re.DOTALL)[0]
    assert re.search(r'<input id="item-modal-name" name="name"', form)
    assert re.search(r'<textarea id="item-modal-description" name="description"', form)
    assert "hx-include" not in form and "hx-vals" not in form
    assert re.search(
        rf'id="item-{item.id}-name"[^>]* data-name="Milk" data-description="2 litres"', html
    )


async def test_item_data_attributes_are_escaped(editor_client, board, db):  # Review Focus 1
    category = await create_category(db, board, NASTY)
    item = await create_item(db, category, NASTY)
    await db.execute(update(Item).where(Item.id == item.id).values(description=NASTY))
    await db.commit()

    for path in (
        f"/boards/{board.id}",
        f"/boards/{board.id}/content",
        f"/categories/{category.id}",
    ):
        html = (await editor_client.get(path)).text
        assert "<script>alert(1)</script>" not in html and "<b>" not in html
        assert f'data-category-name="{NASTY_ESCAPED}"' in html
        assert f'data-name="{NASTY_ESCAPED}" data-description="{NASTY_ESCAPED}"' in html


async def test_done_items_are_struck_through_and_listed_last(editor_client, category, db):
    await create_item(db, category, "Apple", is_done=True)
    await create_item(db, category, "Zucchini")

    html = (await editor_client.get(f"/categories/{category.id}")).text

    items = re.findall(r'<li id="item-\d+"[^>]*class="([^"]*)"[^>]*>.*?</li>', html, re.DOTALL)
    names = re.findall(r'data-name="([^"]*)"', html)
    assert names == ["Zucchini", "Apple"]
    assert "line-through" not in items[0].split()
    assert "line-through" in items[1].split()
    assert re.search(r'<input type="checkbox"[^>]* checked>', html)


async def test_empty_board_invites_the_first_category(editor_client, viewer_client, board):
    for path in (f"/boards/{board.id}", f"/boards/{board.id}/content"):
        editor_html = (await editor_client.get(path)).text
        viewer_html = (await viewer_client.get(path)).text
        assert "Add your first category" in editor_html, path
        assert "Add your first category" not in viewer_html, path  # a viewer cannot
        assert "This board has no categories yet." in viewer_html, path


async def test_empty_category_says_so(editor_client, viewer_client, category, db):
    for client in (editor_client, viewer_client):
        html = section((await client.get(f"/categories/{category.id}")).text, category.id)
        assert "No items yet." in html

    await create_item(db, category, "Milk")
    html = section((await editor_client.get(f"/categories/{category.id}")).text, category.id)
    assert "No items yet." not in html


async def test_clear_done_shows_the_count_only_when_something_is_done(
    editor_client, category, item, db
):
    clear_done = (
        rf'hx-post="/categories/{category.id}/clear-done"[^>]*>Clear done \((\d+)\)</button>'
    )
    html = (await editor_client.get(f"/categories/{category.id}")).text
    assert "/clear-done" not in html  # nothing to clear

    await create_item(db, category, "Apple", is_done=True)
    await create_item(db, category, "Pear", is_done=True)
    html = (await editor_client.get(f"/categories/{category.id}")).text
    assert re.findall(clear_done, html) == ["2"]

    html = (await editor_client.post(f"/items/{item.id}/toggle")).text  # its answer counts too
    assert re.findall(clear_done, html) == ["3"]


async def test_connection_status_is_in_the_header_of_the_board_page(editor_client, board):
    html = (await editor_client.get(f"/boards/{board.id}")).text
    header = re.search(r"<header.*?</header>", html, re.DOTALL)[0]
    assert re.search(rf'<p id="conn-status" data-board-id="{board.id}" role="status"', header)

    assert "conn-status" not in (await editor_client.get("/boards")).text  # board pages only


async def test_bottom_bar_switches_between_the_users_own_boards(
    editor_client, viewer_client, board, db
):
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    home = await create_board(db, editor, NASTY)
    stranger = await create_user(db, "stranger@example.com")
    secret = await create_board(db, stranger, "Secret")

    html = (await editor_client.get(f"/boards/{board.id}")).text

    bar = re.search(r'<nav id="bottom-bar".*?</nav>', html, re.DOTALL)[0]
    links = re.findall(r'<a href="([^"]*)"([^>]*)>([^<]*)</a>', bar)
    assert [(href, text.strip()) for href, _, text in links] == [
        (f"/boards/{home.id}", NASTY_ESCAPED),  # own boards first, as on the board list
        (f"/boards/{board.id}", "Groceries"),
        ("/boards", "All boards"),
    ]
    assert 'aria-current="page"' in links[1][1]
    assert f"/boards/{secret.id}" not in html and "Secret" not in html
    assert re.search(r'<button type="button"[^>]*data-focus="new-category-name"[^>]*>', bar)

    viewer_bar = re.search(
        r'<nav id="bottom-bar".*?</nav>',
        (await viewer_client.get(f"/boards/{board.id}")).text,
        re.DOTALL,
    )[0]
    assert "New category" not in viewer_bar and f"/boards/{board.id}" in viewer_bar
