"""Moving a category, with all its items, to another board: it goes last on the target board under
a name that is free there (or the dialog asks for another), needs edit rights on both boards, and
its items, checklists and files follow it, so only the target board's members reach them."""

import asyncio
import json
import re
from datetime import UTC, datetime

from attachments.samples import upload
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, toast_message, wait_until_a_query_waits_for_a_lock
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.content import routes, service
from easytodo.content.service import (
    CATEGORY_GONE,
    CATEGORY_NAME_TAKEN,
    NAME_TAKEN_ON_TARGET,
    SAME_BOARD,
)
from easytodo.models import Board, BoardRole, Category, CategorySortPref, Item, User
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_HEADER

HX = {"HX-Request": "true"}
NO_PERMISSION = "You don't have permission to do that."
BOARD_GONE = "Board not found."


def move_form(board, name: str = "Dairy") -> dict[str, str]:
    return {"board_id": str(board.id), "name": name}


async def board_and_name_of(db, category) -> tuple[int, str]:
    """Where the category is now and its name (read afresh: ``db`` keeps the objects it made)."""
    row = await db.execute(
        select(Category.board_id, Category.name).where(Category.id == category.id)
    )
    return row.one()


async def test_move_takes_the_category_and_all_its_items_to_the_end(
    editor_client, board, category, item, target_board, db, published
):
    owner = await db.scalar(select(User).where(User.email == "owner@example.com"))
    target_owner = await db.scalar(select(User).where(User.email == "target-owner@example.com"))
    await create_category(db, target_board, "Bakery", position=0)
    await create_category(db, target_board, "Fruit", position=3)
    await create_category(db, target_board, "Old", position=9, archived_at=datetime.now(UTC))
    old_milk = await create_item(db, category, "Old milk")
    await db.execute(  # the author stays, whoever moves it
        update(Item).where(Item.id.in_([item.id, old_milk.id])).values(created_by=owner.id)
    )
    await db.execute(
        update(Item).where(Item.id == old_milk.id).values(deleted_at=datetime.now(UTC))
    )
    await db.commit()

    r = await editor_client.post(
        f"/categories/{category.id}/move",
        data=move_form(target_board, " Fresh   dairy "),
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200
    assert r.content == b""  # the category leaves the page: htmx swaps nothing in
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "info", "message": "Moved to “Weekend”."}
    }
    moved = (
        await db.execute(
            select(Category.board_id, Category.name, Category.position).where(
                Category.id == category.id
            )
        )
    ).one()
    # Last: after the 3 of "Fruit", the archived 9 does not count.
    assert tuple(moved) == (target_board.id, "Fresh dairy", 4)
    items = (
        await db.execute(
            select(Item.name, Item.board_id, Item.category_id, Item.created_by).order_by(Item.id)
        )
    ).all()
    assert [tuple(row) for row in items] == [  # the soft-deleted one too
        ("Milk", target_board.id, category.id, owner.id),
        ("Old milk", target_board.id, category.id, owner.id),
    ]
    assert published == [
        (
            (
                BoardEvent(
                    EventType.CATEGORY_DELETED, board_id=board.id, category_ids=(category.id,)
                ),
                BoardEvent(
                    EventType.CATEGORY_CHANGED,
                    board_id=target_board.id,
                    category_ids=(category.id,),
                ),
                # The bell: the target board's other members first, then those of the board it
                # left (not the editor who moved it).
                BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=target_owner.id),
                BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=owner.id),
            ),
            "tab-1",
        )
    ]
    # The deleted item's "Undo" works on the target board too.
    r = await editor_client.post(f"/items/{old_milk.id}/restore", headers=HX)
    assert r.status_code == 200


async def test_moved_category_items_follow_the_board(  # Review Focus 2
    editor_client, viewer_client, signed_in_client, board, category, item, target_board, db
):
    entry = await create_checklist_entry(db, item, "Eggs")
    attachment_id = await upload(
        editor_client, item.id, filename="note.txt", content=b"Oat milk.\n"
    )
    reads = [
        f"/categories/{category.id}",
        f"/items/{item.id}/details",
        f"/attachments/{attachment_id}",
        f"/attachments/{attachment_id}/file",
    ]
    for path in reads:  # the test would prove nothing if the viewer could not reach them before
        assert (await viewer_client.get(path)).status_code == 200, path

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
    )
    assert r.status_code == 200

    for path in reads:
        r = await viewer_client.get(path, headers=HX)
        assert r.status_code == 404, path
        assert "Eggs" not in r.text and "note.txt" not in r.text
    for path in (f"/items/{item.id}/toggle", f"/checklist/{entry.id}/check"):
        r = await viewer_client.post(path, headers=HX)
        assert r.status_code == 404, path  # not 403: the source board's viewer is no member now
    assert (await viewer_client.get(f"/boards/{board.id}/content")).text.count("Milk") == 0

    target_owner = await signed_in_client("target-owner@example.com")
    details = await target_owner.get(f"/items/{item.id}/details")
    assert details.status_code == 200
    assert "Eggs" in details.text and "note.txt" in details.text
    assert f'id="item-{item.id}"' in (await target_owner.get(f"/boards/{target_board.id}")).text
    file = await target_owner.get(f"/attachments/{attachment_id}/file")
    assert (file.status_code, file.content) == (200, b"Oat milk.\n")
    r = await target_owner.post(f"/items/{item.id}/toggle", headers=HX)
    assert r.status_code == 200  # and the item is the target board's to change


async def test_move_keeps_updated_at_and_sort_preferences(
    editor_client, category, item, target_board, db
):
    await create_item(db, category, "Butter")
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    db.add(
        CategorySortPref(
            user_id=editor.id,
            category_id=category.id,
            field="name",
            descending=True,
            open_first=True,
        )
    )
    await db.commit()
    before = (await db.execute(select(Item.id, Item.updated_at).order_by(Item.id))).all()

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
    )

    assert r.status_code == 200
    # Moving is no edit of the items: "Date modified" would otherwise list them all as just changed.
    after = (await db.execute(select(Item.id, Item.updated_at).order_by(Item.id))).all()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    prefs = (await db.execute(select(CategorySortPref.category_id, CategorySortPref.field))).all()
    assert [tuple(row) for row in prefs] == [(category.id, "name")]  # the editor's own order stays
    html = (await editor_client.get(f"/categories/{category.id}")).text
    assert re.findall(r'data-name="([^"]*)"', html) == ["Milk", "Butter"]  # Z–A, as chosen


async def test_taken_name_is_409_with_a_suggestion(
    editor_client, category, target_board, db, content_snapshot
):
    await create_category(db, target_board, "dairy")
    await create_category(db, target_board, "Dairy (2)", archived_at=datetime.now(UTC))
    before = await content_snapshot()

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
    )

    assert r.status_code == 409
    assert r.content == b""
    assert r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {  # no "resync": nothing is out of date
        "toast": {"level": "error", "message": NAME_TAKEN_ON_TARGET.format(name="Dairy")},
        "categoryNameTaken": {"suggestion": "Dairy (2)"},  # the archived one has freed its name
    }
    assert NAME_TAKEN_ON_TARGET.format(name="Dairy") == (
        "A category named “Dairy” already exists on that board."
    )
    assert await content_snapshot() == before

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board, "Dairy (2)"), headers=HX
    )
    assert r.status_code == 200
    assert await board_and_name_of(db, category) == (target_board.id, "Dairy (2)")


async def test_move_needs_edit_rights_on_both_boards(
    editor_client,
    viewer_client,
    signed_in_client,
    board,
    category,
    target_board,
    db,
    content_snapshot,
):
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    stranger = await create_user(db, "stranger@example.com")
    foreign = await create_board(db, stranger, "Private")
    read_only = await create_board(db, stranger, "Read only", {editor: BoardRole.VIEWER})
    archived = await create_board(
        db, stranger, "Old plans", {editor: BoardRole.EDITOR}, archived_at=datetime.now(UTC)
    )
    before = await content_snapshot()
    path = f"/categories/{category.id}/move"

    r = await viewer_client.post(path, data=move_form(target_board), headers=HX)
    assert (r.status_code, toast_message(r)) == (403, NO_PERMISSION)  # a viewer of the source
    r = await editor_client.post(path, data=move_form(read_only), headers=HX)
    assert (r.status_code, toast_message(r)) == (403, NO_PERMISSION)  # a viewer of the target
    token = editor_client.headers.pop(CSRF_HEADER)
    r = await editor_client.post(path, data=move_form(target_board), headers=HX)
    assert r.status_code == 403  # no CSRF token
    editor_client.headers[CSRF_HEADER] = token

    for target_id in (foreign.id, archived.id, 0):  # not a member, archived, no such board
        r = await editor_client.post(
            path, data={"board_id": str(target_id), "name": "Dairy"}, headers=HX
        )
        assert r.status_code == 404, target_id
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": BOARD_GONE},
            "resync": True,
        }

    r = await editor_client.post(path, data=move_form(board), headers=HX)  # already there
    assert r.status_code == 422
    assert toast_message(r) == SAME_BOARD == "Choose another board."
    for name in ("", "   ", "x" * 121):
        r = await editor_client.post(path, data=move_form(target_board, name), headers=HX)
        assert r.status_code == 422, name
        assert toast_message(r) == "Category name must be 1–120 characters."
    assert await content_snapshot() == before


async def test_viewer_move_is_403_without_waiting_for_a_row_lock(
    viewer_client, category, target_board, db
):
    """An editor's change holds the category locked: a viewer's move is refused at once instead
    of queueing behind it (and, had it got there first, would hold up the editor)."""
    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text("SELECT 1 FROM categories WHERE id = :id FOR UPDATE"), {"id": category.id}
        )
        request = viewer_client.post(
            f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
        )
        r = await asyncio.wait_for(request, 2)

    assert r.status_code == 403
    assert toast_message(r) == NO_PERMISSION


async def test_move_racing_an_archive_on_another_device_is_404_not_500(  # Review Focus 4
    editor_client, board, category, target_board, db
):
    """The other device's archiving holds the category's row lock until it commits: the move
    waits for it and then finds the category gone."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            update(Category).where(Category.id == category.id).values(archived_at=datetime.now(UTC))
        )
        move = asyncio.create_task(
            editor_client.post(
                f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
            )
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await move

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": CATEGORY_GONE},
        "resync": True,
    }
    assert await board_and_name_of(db, category) == (board.id, "Dairy")  # archived, not moved


async def test_target_deleted_meanwhile_is_404_not_500(
    editor_client, board, category, target_board, db, monkeypatch, _db_engine
):
    """The target board goes (another device deleted it) between the permission check and the
    update: the foreign key refuses the row, which is a 404 like any other vanished board."""
    check = service.require_board

    async def check_then_delete(session, board_id, user, action):
        found = await check(session, board_id, user, action)
        async with AsyncSession(_db_engine) as other:
            await other.execute(delete(Board).where(Board.id == board_id))
            await other.commit()
        return found

    monkeypatch.setattr(service, "require_board", check_then_delete)

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
    )

    assert r.status_code == 404
    assert toast_message(r) == BOARD_GONE
    assert await board_and_name_of(db, category) == (board.id, "Dairy")  # still where it was


async def test_target_deleted_after_the_move_still_says_it_was_moved(
    editor_client, board, category, target_board, db, monkeypatch, _db_engine
):
    """The move is committed when the board that took the category goes (another device deleted
    it before the answer names it): the toast says "another board" rather than failing."""
    move = routes.move_category

    async def move_then_delete_the_target(*args, **kwargs):
        moved = await move(*args, **kwargs)
        async with AsyncSession(_db_engine) as other:
            await other.execute(delete(Board).where(Board.id == target_board.id))
            await other.commit()
        return moved

    monkeypatch.setattr(routes, "move_category", move_then_delete_the_target)

    r = await editor_client.post(
        f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
    )

    assert r.status_code == 200
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "info", "message": "Moved to another board."}
    }


async def test_second_of_two_moves_of_the_same_category_is_404_not_500(
    editor_client, category, target_board, db
):
    """Another device's move of the category, not committed yet, holds its row: ours waits, then
    finds the category on another board, which is a 404 with a resync like any change that lost
    a race."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("UPDATE categories SET board_id = :board, position = 7 WHERE id = :id"),
            {"board": target_board.id, "id": category.id},
        )
        move = asyncio.create_task(
            editor_client.post(
                f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
            )
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await move

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": CATEGORY_GONE},
        "resync": True,
    }
    assert await board_and_name_of(db, category) == (target_board.id, "Dairy")  # the other's move


async def test_move_waits_for_an_item_being_added_and_takes_it_along(
    editor_client, board, category, item, target_board, db
):
    """An item added to the category at the same time (not committed yet) holds the category's
    key: the move waits for it, and the new item lands on the target board with the rest."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text(
                "INSERT INTO items (board_id, category_id, name) VALUES (:board, :category, 'Late')"
            ),
            {"board": board.id, "category": category.id},
        )
        move = asyncio.create_task(
            editor_client.post(
                f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
            )
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await move

    assert r.status_code == 200
    items = (await db.execute(select(Item.name, Item.board_id).order_by(Item.id))).all()
    assert [tuple(row) for row in items] == [
        ("Milk", target_board.id),
        ("Late", target_board.id),
    ]


async def test_move_waits_for_a_tick_and_keeps_it(editor_client, category, item, target_board, db):
    """A tick on an item of the category (not committed yet) holds the item: the move waits for it,
    and the item arrives on the target board ticked."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("UPDATE items SET is_done = true WHERE id = :id"), {"id": item.id}
        )
        move = asyncio.create_task(
            editor_client.post(
                f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
            )
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await move

    assert r.status_code == 200
    items = (await db.execute(select(Item.name, Item.board_id, Item.is_done))).all()
    assert [tuple(row) for row in items] == [("Milk", target_board.id, True)]


async def test_name_taken_at_commit_is_a_plain_409_and_nothing_moves(
    editor_client, board, category, target_board, db
):
    """Another device adds a category of that name to the target board (not committed yet) after
    the move found the name free: the unique index refuses it at the update. That is the ordinary
    "name taken" 409, without the suggestion (there was none to make), and nothing moves."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("INSERT INTO categories (board_id, name, position) VALUES (:board, 'Dairy', 0)"),
            {"board": target_board.id},
        )
        move = asyncio.create_task(
            editor_client.post(
                f"/categories/{category.id}/move", data=move_form(target_board), headers=HX
            )
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await move

    assert r.status_code == 409
    assert r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {  # no categoryNameTaken, no resync
        "toast": {"level": "error", "message": CATEGORY_NAME_TAKEN}
    }
    assert await board_and_name_of(db, category) == (board.id, "Dairy")


async def test_move_menu_and_dialog_markup(
    editor_client, signed_in_client, viewer_client, board, category, target_board, db
):
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    stranger = await create_user(db, "stranger@example.com")
    await create_board(db, stranger, "Read only", {editor: BoardRole.VIEWER})
    await create_board(
        db, stranger, "Old plans", {editor: BoardRole.EDITOR}, archived_at=datetime.now(UTC)
    )

    fragment = (await editor_client.get(f"/categories/{category.id}")).text
    assert fragment.count(f'data-move-category-id="{category.id}"') == 1
    assert (
        f'<button type="button" class="menu-item" data-move-category-id="{category.id}"'
        ' data-move-category-label="Dairy" aria-haspopup="dialog">Move to board…</button>'
    ) in fragment
    assert fragment.index("Move to board…") < fragment.index("Archive category")
    # Only the section carries the category's name for the item modal's list of categories.
    assert fragment.count("data-category-name=") == 1

    page = (await editor_client.get(f"/boards/{board.id}")).text
    content = (await editor_client.get(f"/boards/{board.id}/content")).text
    assert page.count('id="move-category"') == 1
    assert 'x-data="moveCategory"' in page
    assert 'id="move-category"' not in content  # the dialog is outside what is swapped
    dialog = page[page.index('id="move-category"') :]
    assert re.findall(r"<option[^>]*>(.*?)</option>", dialog) == ["Weekend"]  # only a board to edit
    assert f'<option value="{target_board.id}">' in dialog
    for markup in (
        'role="dialog" aria-modal="true" aria-labelledby="move-category-title"',
        'id="move-category-board" name="board_id"',
        'id="move-category-name" name="name"',
        'maxlength="120"',
        'id="move-category-taken" role="alert"',
        "Move to board</h2>",
        ">Cancel</button>",
        ">Move</button>",
    ):
        assert markup in dialog, markup

    owner = await signed_in_client("owner@example.com")  # a single board: nowhere to move to
    assert 'id="move-category"' not in (await owner.get(f"/boards/{board.id}")).text
    assert "Move to board…" in (await owner.get(f"/categories/{category.id}")).text  # CSS hides it

    for path in (f"/boards/{board.id}", f"/categories/{category.id}"):
        html = (await viewer_client.get(path)).text
        assert "Move to board" not in html and "move-category" not in html, path


async def test_move_names_are_escaped(signed_in_client, db):  # Review Focus 3
    owner = await create_user(db, "owner@example.com")
    source = await create_board(db, owner, "Groceries")
    target = await create_board(db, owner, NASTY)
    category = await create_category(db, source, NASTY)
    await create_category(db, target, NASTY)
    client = await signed_in_client("owner@example.com")

    page = (await client.get(f"/boards/{source.id}")).text
    assert f'<option value="{target.id}">{NASTY_ESCAPED}</option>' in page
    assert f'data-move-category-label="{NASTY_ESCAPED}"' in page
    assert "<script>alert(1)</script>" not in page and NASTY not in page

    path = f"/categories/{category.id}/move"
    r = await client.post(path, data={"board_id": str(target.id), "name": NASTY}, headers=HX)
    assert r.status_code == 409
    assert r.headers["hx-trigger"].isascii()  # a header: the JSON escapes what is not ASCII
    trigger = json.loads(r.headers["hx-trigger"])
    assert trigger["toast"]["message"] == NAME_TAKEN_ON_TARGET.format(name=NASTY)
    assert trigger["categoryNameTaken"] == {"suggestion": f"{NASTY} (2)"}

    r = await client.post(
        path, data={"board_id": str(target.id), "name": f"{NASTY} (2)"}, headers=HX
    )
    assert r.status_code == 200
    assert toast_message(r) == f"Moved to “{NASTY}”."
