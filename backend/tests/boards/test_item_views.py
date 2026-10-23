"""``select_item_views``, the one query that lists items: their author (shown as an avatar on a
shared board only), checklist and attachment counts, order, and what it leaves out (archived
categories, deleted items, other boards); and the avatar in the item's markup."""

import re
from datetime import UTC, datetime

import pytest
from factories import (
    create_board,
    create_category,
    create_checklist_entry,
    create_item,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, count_queries, signed_in
from sqlalchemy import delete, select, update

from easytodo.boards.service import Author, ItemView, select_item_views
from easytodo.models import Attachment, Board, BoardMember, BoardRole, Category, Item, User

HX = {"HX-Request": "true"}
# The avatar is the last child of the item's <li>, after the block with the name.
AVATAR_AFTER_THE_NAME = re.compile(
    r">Milk</button>\s*</div>\s*"
    r'<div class="shrink-0 py-2\.5 pl-2 \*:align-top">\s*'
    r'<span class="avatar[^"]*" aria-hidden="true" title="owner">O</span>\s*</div>\s*</li>'
)


async def add_member(db, board: Board, user: User, role: BoardRole = BoardRole.EDITOR) -> None:
    db.add(BoardMember(board_id=board.id, user_id=user.id, role=role))
    await db.commit()


async def views(db, user: User, *, board: Board | None = None, category: Category | None = None):
    """The views of the board (or of the category), read afresh: what the session remembers of
    the rows does not count."""
    db.expunge_all()
    scope = {"board_id": board.id} if board else {"category_id": category.id}
    return await select_item_views(db, user, **scope)


async def authors(db, user: User, board: Board, category: Category):
    """``(author, show_author)`` of every item of the category, which must be the same read by the
    category and by the whole board (the board has no other category here)."""
    by_category = await views(db, user, category=category)
    by_board = await views(db, user, board=board)
    assert [iv.item.id for iv in by_category] == [iv.item.id for iv in by_board]
    assert [(iv.author, iv.show_author) for iv in by_category] == [
        (iv.author, iv.show_author) for iv in by_board
    ]
    return [(iv.author, iv.show_author) for iv in by_category]


def attach(db, item: Item, number: int) -> None:
    db.add(
        Attachment(
            item_id=item.id,
            filename=f"receipt-{number}.png",
            kind="image",
            content_type="image/png",
            size_bytes=12,
            storage_key=f"{number:032}",
        )
    )


async def test_new_item_records_its_author(editor_client, board, db):
    category = await create_category(db, board, "Dairy")

    r = await editor_client.post(
        f"/categories/{category.id}/items", data={"name": "Butter"}, headers=HX
    )

    assert r.status_code == 200
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    assert await db.scalar(select(Item.created_by).where(Item.name == "Butter")) == editor.id
    assert 'title="editor">E</span>' in r.text  # the board is shared: the avatar shows


async def test_author_shows_only_on_shared_boards(db, owner, board):
    category = await create_category(db, board)
    await create_item(db, category, "Milk", created_by=owner)

    assert await authors(db, owner, board, category) == [(Author(owner.id, "owner"), False)]

    editor = await create_user(db, "editor@example.com")
    await add_member(db, board, editor)
    assert await authors(db, owner, board, category) == [(Author(owner.id, "owner"), True)]

    await create_item(db, category, "Bread")  # no author: nobody to show
    assert await authors(db, owner, board, category) == [
        (None, False),  # Bread
        (Author(owner.id, "owner"), True),  # Milk
    ]

    await db.execute(delete(BoardMember).where(BoardMember.user_id == editor.id))
    await db.commit()
    assert await authors(db, owner, board, category) == [
        (None, False),
        (Author(owner.id, "owner"), False),  # alone on the board again
    ]

    await db.execute(update(User).where(User.id == owner.id).values(display_name="Anna Kowalska"))
    await db.commit()
    assert (await authors(db, owner, board, category))[1] == (
        Author(owner.id, "Anna Kowalska"),
        False,
    )


async def test_deleting_the_author_keeps_the_item_without_an_author(db, owner, board):
    category = await create_category(db, board)
    anna = await create_user(db, "anna@example.com")
    bob = await create_user(db, "bob@example.com")
    await add_member(db, board, anna)
    await add_member(db, board, bob)
    await create_item(db, category, "Milk", created_by=anna)
    assert await authors(db, owner, board, category) == [(Author(anna.id, "anna"), True)]

    await db.execute(delete(User).where(User.id == anna.id))
    await db.commit()

    # Still shared (the owner and Bob), but nobody to show.
    assert await authors(db, owner, board, category) == [(None, False)]


async def test_counts_checklist_entries_and_attachments(db, owner, board):
    category = await create_category(db, board)
    full = await create_item(db, category, "A full task")
    for position, done in enumerate((True, False, True)):
        await create_checklist_entry(db, full, f"Step {position}", is_done=done, position=position)
    attach(db, full, 1)
    attach(db, full, 2)
    await db.commit()
    await create_item(db, category, "B empty task")

    for scope in ({"category": category}, {"board": board}):
        full_view, empty_view = await views(db, owner, **scope)
        assert (
            full_view.checklist_done,
            full_view.checklist_total,
            full_view.attachment_count,
        ) == (
            2,
            3,
            2,
        )
        assert (
            empty_view.checklist_done,
            empty_view.checklist_total,
            empty_view.attachment_count,
        ) == (0, 0, 0)


async def test_item_views_take_one_statement_for_a_category_and_for_a_board(db, owner, board):
    fruit = await create_category(db, board, "Fruit", position=0)
    dairy = await create_category(db, board, "Dairy", position=1)
    await create_item(db, fruit, "banana", is_done=True)
    await create_item(db, fruit, "Apple")
    await create_item(db, fruit, "cherry")
    await create_item(db, dairy, "milk")
    archived = await create_category(
        db, board, "Archived", position=2, archived_at=datetime.now(UTC)
    )
    await create_item(db, archived, "In an archived category")
    await create_item(db, fruit, "Deleted")
    await db.execute(
        update(Item).where(Item.name == "Deleted").values(deleted_at=datetime.now(UTC))
    )
    await db.commit()
    other = await create_category(db, await create_board(db, owner, "Other"), "Elsewhere")
    await create_item(db, other, "On another board")
    db.expunge_all()

    with count_queries(db) as statements:
        in_category = await select_item_views(db, owner, category_id=fruit.id)
    assert len(statements) == 1
    assert [iv.item.name for iv in in_category] == ["Apple", "cherry", "banana"]
    assert all(isinstance(iv, ItemView) for iv in in_category)

    with count_queries(db) as statements:
        on_board = await select_item_views(db, owner, board_id=board.id)
    assert len(statements) == 1
    assert [iv.item.name for iv in on_board] == ["Apple", "cherry", "milk", "banana"]


async def test_select_item_views_needs_exactly_one_scope(db, owner, board):
    category = await create_category(db, board)

    with pytest.raises(TypeError):
        await select_item_views(db, owner)
    with pytest.raises(TypeError):
        await select_item_views(db, owner, board_id=board.id, category_id=category.id)


async def test_tile_shows_the_avatar_after_the_name(
    editor_client, client_factory, owner, board, db
):
    category = await create_category(db, board, "Dairy")
    await create_item(db, category, "Milk", created_by=owner)
    viewer = await create_user(db, "viewer@example.com")
    await add_member(db, board, viewer, BoardRole.VIEWER)
    paths = [f"/boards/{board.id}", f"/boards/{board.id}/content", f"/categories/{category.id}"]

    for path in paths:
        html = (await editor_client.get(path)).text
        assert AVATAR_AFTER_THE_NAME.search(html), path

    viewer_client = await signed_in(client_factory, "viewer@example.com")
    for path in paths:
        html = (await viewer_client.get(path)).text
        assert 'aria-hidden="true" title="owner">O</span>' in html, path  # the name is a <p> here


async def test_one_member_board_shows_no_avatars(client_factory, owner, board, db):
    category = await create_category(db, board, "Dairy")
    await create_item(db, category, "Milk", created_by=owner)
    client = await signed_in(client_factory, "owner@example.com")

    for path in (
        f"/boards/{board.id}",
        f"/boards/{board.id}/content",
        f"/categories/{category.id}",
    ):
        html = (await client.get(path)).text
        assert "Milk" in html
        assert 'class="avatar' not in html, path


async def test_display_name_is_escaped_in_the_avatar(
    editor_client, owner, board, db
):  # Review Focus 3
    await db.execute(update(User).where(User.id == owner.id).values(display_name=NASTY))
    await db.commit()
    category = await create_category(db, board, "Dairy")
    await create_item(db, category, "Milk", created_by=owner)

    html = (await editor_client.get(f"/boards/{board.id}")).text

    assert f'title="{NASTY_ESCAPED}">O&lt;</span>' in html
    assert "<b>" not in html
