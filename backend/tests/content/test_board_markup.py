"""The board's HTML: edit controls only for members who may edit, fragments that match the full
page, item data passed to the modal only through escaped data-* attributes, and templates that
keep to the CSP (no inline scripts, handlers or styles)."""

import re

from factories import create_category, create_item
from sqlalchemy import update

from easytodo.models import Item
from easytodo.web import FRONTEND_DIR

HX = {"HX-Request": "true"}
NASTY = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
NASTY_ESCAPED = "O&#39;Reilly &lt;b&gt;&#34;x&#34;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;"
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
    assert f'hx-post="/categories/{category.id}/clear-done"' in html
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
    assert f'hx-delete="/boards/{board.id}"' in owner_html

    editor_html = (await editor_client.get(f"/boards/{board.id}/content", headers=HX)).text
    assert re.search(r"<h1[^>]*>Groceries</h1>", editor_html)
    assert f'hx-patch="/boards/{board.id}"' not in editor_html
    assert f'hx-delete="/boards/{board.id}"' not in editor_html
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
