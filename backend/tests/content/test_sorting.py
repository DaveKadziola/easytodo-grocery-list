"""Sort order per user: the account default (name, date added or date modified; ascending or
descending; open items first or not), a category's own order that beats it, "Use my default", and
the "⇅" control on every category for every role. The order is the viewer's own: it changes
nothing for the other members and publishes no event."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from factories import create_board, create_category, create_item, create_user
from helpers import NASTY, NASTY_ESCAPED, count_queries, wait_until_a_query_waits_for_a_lock
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from content.test_board_markup import EDIT_CONTROLS, section
from easytodo.boards.service import SortChoice, get_board_view
from easytodo.content import prefs, prefs_routes
from easytodo.content.prefs import get_category_sort
from easytodo.content.service import CATEGORY_GONE, get_category_view
from easytodo.models import Category, CategorySortPref, Item, User
from easytodo.security.csrf import CSRF_HEADER

HX = {"HX-Request": "true"}
VALID = {"sort_field": "name", "sort_direction": "desc", "sort_open_first": "true"}
HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)

# Ascending, among the open items of seed_fruit ("Almond" is done, so it comes last when open
# items go first); descending is the reverse.
NAME_DESCENDING = {"field": "name", "descending": True, "open_first": True}
CREATED_DESCENDING = {"field": "created", "descending": True, "open_first": False}
ASCENDING = {
    "name": ["apple", "Banana", "Cherry"],
    "created": ["Cherry", "apple", "Banana"],
    "updated": ["Banana", "Cherry", "apple"],
}
SORT_CASES = [
    (field, descending, order[::-1] if descending else order)
    for field, order in ASCENDING.items()
    for descending in (False, True)
]


async def seed_fruit(db, category) -> None:
    """apple (added t1, changed t2), Banana (t2, t0), Cherry (t0, t1) and the done "Almond" (older
    than any of them); the one UPDATE sets both times (the column's onupdate would write the
    current time)."""
    for name, created, updated, done in (
        ("apple", T1, T2, False),
        ("Banana", T2, T0, False),
        ("Cherry", T0, T1, False),
        ("Almond", T0 - HOUR, T0 - HOUR, True),  # the oldest of all: no tie with an open item
    ):
        item = await create_item(db, category, name, is_done=done)
        await db.execute(
            update(Item).where(Item.id == item.id).values(created_at=created, updated_at=updated)
        )
    await db.commit()


async def user_named(db, email: str) -> User:
    return await db.scalar(select(User).where(User.email == email))


async def set_account_default(db, email: str, field: str, descending: bool, open_first=True):
    await db.execute(
        update(User)
        .where(User.email == email)
        .values(sort_field=field, sort_descending=descending, sort_open_first=open_first)
    )
    await db.commit()
    db.expunge_all()


async def names_on_page(client, db, path: str) -> list[str]:
    """The names of the items on ``path`` (a page or a fragment), in the order it lists them."""
    names = dict((await db.execute(select(Item.id, Item.name))).all())
    html = (await client.get(path)).text
    return [names[int(i)] for i in re.findall(r'<li id="item-(\d+)"', html)]


def names_in(category_view) -> list[str]:
    return [iv.item.name for iv in category_view.items]


async def both_views(db, board, category, user):
    """The category as ``get_category_view`` and as ``get_board_view`` build it."""
    db.expunge_all()
    view, _ = await get_category_view(db, category.id, user)
    board_view = await get_board_view(db, board.id, user)
    (on_board,) = [c for c in board_view.categories if c.category.id == category.id]
    return view, on_board


async def test_default_order_is_unchanged(db, board, category):
    owner = await user_named(db, "owner@example.com")
    await create_item(db, category, "banana", is_done=True)
    await create_item(db, category, "Apple")
    await create_item(db, category, "cherry")
    await create_item(db, category, "apple")

    for view in await both_views(db, board, category, owner):
        assert names_in(view) == ["Apple", "apple", "cherry", "banana"]
        assert view.sort == SortChoice("name", False, True, False)


@pytest.mark.parametrize(("field", "descending", "expected"), SORT_CASES)
async def test_account_default_orders_by_each_field_and_direction(
    db, board, category, field, descending, expected
):
    await seed_fruit(db, category)
    await set_account_default(db, "owner@example.com", field, descending)
    owner = await user_named(db, "owner@example.com")

    for view in await both_views(db, board, category, owner):
        assert names_in(view) == [*expected, "Almond"]
        assert view.sort == SortChoice(field, descending, True, False)


async def test_open_first_off_mixes_done_items_in(db, board, category):
    await seed_fruit(db, category)
    await set_account_default(db, "owner@example.com", "name", False, open_first=False)
    owner = await user_named(db, "owner@example.com")

    for view in await both_views(db, board, category, owner):
        assert names_in(view) == ["Almond", "apple", "Banana", "Cherry"]
        assert view.sort == SortChoice("name", False, False, False)


async def test_category_sort_beats_the_account_default_and_is_per_user(
    editor_client, signed_in_client, category, db
):
    await seed_fruit(db, category)
    await set_account_default(db, "editor@example.com", "updated", False)
    await set_account_default(db, "owner@example.com", "created", True)
    owner_client = await signed_in_client("owner@example.com")
    path = f"/categories/{category.id}"
    assert await names_on_page(editor_client, db, path) == ["Banana", "Cherry", "apple", "Almond"]

    r = await editor_client.post(f"{path}/sort", data=VALID, headers=HX)

    assert r.status_code == 200
    assert await names_on_page(editor_client, db, path) == ["Cherry", "Banana", "apple", "Almond"]
    # Not the owner's: their account default stands, and they still see each item once.
    assert await names_on_page(owner_client, db, path) == ["Banana", "apple", "Cherry", "Almond"]
    editor = await user_named(db, "editor@example.com")
    first = await get_category_sort(db, category.id, editor)
    assert (first.user_id, first.field, first.descending, first.open_first) == (
        editor.id,
        "name",
        True,
        True,
    )

    other = {"sort_field": "created", "sort_direction": "asc", "sort_open_first": "false"}
    assert (await editor_client.post(f"{path}/sort", data=other, headers=HX)).status_code == 200

    # The same session reads the row again: what it holds of it is not what it gets.
    saved = await get_category_sort(db, category.id, editor)
    assert (saved.field, saved.descending, saved.open_first) == ("created", False, False)
    assert len(await all_prefs(db)) == 1  # updated, not added
    assert await names_on_page(editor_client, db, path) == ["Almond", "Cherry", "apple", "Banana"]
    assert await names_on_page(owner_client, db, path) == ["Banana", "apple", "Cherry", "Almond"]


async def all_prefs(db) -> list[CategorySortPref]:
    db.expunge_all()
    return list(await db.scalars(select(CategorySortPref).order_by(CategorySortPref.user_id)))


async def test_reset_returns_to_the_account_default(editor_client, category, db):
    path = f"/categories/{category.id}"
    editor = await user_named(db, "editor@example.com")
    r = await editor_client.post(f"{path}/sort", data=VALID, headers=HX)
    assert "sort-custom" in r.text
    assert await get_category_sort(db, category.id, editor) is not None

    r = await editor_client.post(f"{path}/sort/reset", headers=HX)

    assert r.status_code == 200
    assert "sort-custom" not in r.text
    assert await get_category_sort(db, category.id, editor) is None
    assert (await editor_client.post(f"{path}/sort/reset", headers=HX)).status_code == 200


async def test_sort_changes_publish_no_event(editor_client, category, monkeypatch):
    calls: list[tuple] = []
    commit_and_publish = prefs.commit_and_publish

    async def spy(db, *events):
        calls.append(events)
        await commit_and_publish(db, *events)

    monkeypatch.setattr(prefs, "commit_and_publish", spy)

    for path, data in (("sort", VALID), ("sort/reset", None)):
        r = await editor_client.post(f"/categories/{category.id}/{path}", data=data, headers=HX)
        assert r.status_code == 200

    assert calls == [(), ()]


async def test_viewer_can_sort_but_still_cannot_edit(
    viewer_client, category, item, db, content_snapshot
):
    before = await content_snapshot()
    path = f"/categories/{category.id}"

    for url, data in ((f"{path}/sort", VALID), (f"{path}/sort/reset", None)):
        r = await viewer_client.post(url, data=data, headers=HX)
        assert r.status_code == 200, url
        assert "Milk" in r.text
        for control in EDIT_CONTROLS:
            assert control not in r.text, (url, control)

    r = await viewer_client.patch(path, data={"name": "Hijacked"}, headers=HX)
    assert r.status_code == 403
    assert await content_snapshot() == before


@pytest.mark.parametrize(
    "data",
    [{"sort_field": "size"}, {"sort_direction": "up"}, {"sort_open_first": "maybe"}, None],
)
async def test_invalid_sort_is_422(editor_client, category, db, data):
    form = {} if data is None else VALID | data

    r = await editor_client.post(f"/categories/{category.id}/sort", data=form, headers=HX)

    assert r.status_code == 422
    assert json.loads(r.headers["hx-trigger"])["toast"] == {
        "level": "error",
        "message": "Choose a valid sort order.",
    }
    assert await all_prefs(db) == []


async def test_missing_open_first_means_no(editor_client, category, db):
    form = {"sort_field": "created", "sort_direction": "desc"}

    r = await editor_client.post(f"/categories/{category.id}/sort", data=form, headers=HX)

    assert r.status_code == 200
    (pref,) = await all_prefs(db)
    assert (pref.field, pref.descending, pref.open_first) == ("created", True, False)


async def test_sort_routes_are_404_for_foreign_ids(signed_in_client, db):  # Review Focus 1
    assert {
        (method, route.path) for route in prefs_routes.router.routes for method in route.methods
    } == {
        ("POST", "/categories/{category_id}/sort"),
        ("POST", "/categories/{category_id}/sort/reset"),
    }
    alice = await create_user(db, "alice@example.com")
    bob = await create_user(db, "bob@example.com")
    alices = await create_category(db, await create_board(db, alice, "Alice's list"), "Dairy")
    await create_item(db, alices, "Milk")
    bobs_board = await create_board(db, bob, "Bob's list")
    archived = await create_category(db, bobs_board, "Bob's old", archived_at=T0)
    old_board = await create_board(db, bob, "Bob's old list", archived_at=T0)
    on_old_board = await create_category(db, old_board, "Bob's shelf")
    db.add(CategorySortPref(user_id=alice.id, category_id=alices.id, **CREATED_DESCENDING))
    await db.commit()
    client = await signed_in_client("bob@example.com")
    ids = [alices.id, 999999, 0, -1, 2**63, "abc", archived.id, on_old_board.id]

    for category_id in ids:
        for tail in ("sort", "sort/reset"):
            for headers in ({}, HX):
                r = await client.post(
                    f"/categories/{category_id}/{tail}", data=VALID, headers=headers
                )
                assert r.status_code == 404, (category_id, tail, headers)
                for name in ("Alice", "Dairy", "Milk", "Bob's"):
                    assert name not in r.text, (category_id, tail, name)
                if headers:
                    assert json.loads(r.headers["hx-trigger"])["resync"] is True

    assert [(p.user_id, p.category_id) for p in await all_prefs(db)] == [(alice.id, alices.id)]


@pytest.mark.parametrize("headers", [{}, HX], ids=["page", "htmx"])
async def test_category_deleted_after_the_lookup_is_404(
    editor_client, category, db, monkeypatch, headers
):
    """Looking the category up takes no lock, so another device can delete it before the row is
    written: the foreign key refuses the write, and the answer is the same 404 as for a category
    that was gone before (with a resync for the page)."""
    load_category = prefs.load_category

    async def load_then_delete_elsewhere(*args, **kwargs):
        found = await load_category(*args, **kwargs)
        await db.execute(delete(Category).where(Category.id == category.id))
        await db.commit()  # as the other device's request does
        return found

    monkeypatch.setattr(prefs, "load_category", load_then_delete_elsewhere)

    r = await editor_client.post(f"/categories/{category.id}/sort", data=VALID, headers=headers)

    assert r.status_code == 404
    if headers:
        assert json.loads(r.headers["hx-trigger"]) == {
            "toast": {"level": "error", "message": CATEGORY_GONE},
            "resync": True,
        }
    else:
        assert CATEGORY_GONE in r.text
    assert await db.scalar(select(func.count()).select_from(Category)) == 0  # it did go
    assert await all_prefs(db) == []


async def test_two_sorts_of_the_same_category_at_once_leave_one_row(
    editor_client, category, db, monkeypatch
):
    """The same member sorting the same category from two devices at the same moment: the second
    write waits for the first one's commit, and then updates its row (one row, no error)."""
    editor = await user_named(db, "editor@example.com")
    commit_and_publish = prefs.commit_and_publish
    first_is_writing, release_first = asyncio.Event(), asyncio.Event()
    commits = 0

    async def hold_the_first_commit(session, *events):
        nonlocal commits
        commits += 1
        if commits == 1:  # the first writer, whose row is in but not committed
            first_is_writing.set()
            await release_first.wait()
        await commit_and_publish(session, *events)

    monkeypatch.setattr(prefs, "commit_and_publish", hold_the_first_commit)

    async def sort(field: str, descending: bool) -> None:
        async with AsyncSession(db.bind, expire_on_commit=False) as session:
            await prefs.set_category_sort(
                session, category.id, editor, field=field, descending=descending, open_first=True
            )

    async def race() -> None:
        first = asyncio.create_task(sort("name", False))
        await first_is_writing.wait()
        second = asyncio.create_task(sort("created", True))
        await wait_until_a_query_waits_for_a_lock(db)  # the second one's upsert, behind the first
        release_first.set()
        await asyncio.gather(first, second)  # neither raises

    await asyncio.wait_for(race(), timeout=15)

    (pref,) = await all_prefs(db)
    assert (pref.user_id, pref.category_id) == (editor.id, category.id)
    assert (pref.field, pref.descending) == ("created", True)  # the last one to write


@pytest.mark.parametrize("breach", ["no_token", "foreign_origin"])
async def test_sort_routes_require_csrf_and_own_origin(editor_client, category, db, breach):
    if breach == "no_token":
        del editor_client.headers[CSRF_HEADER]
    else:
        editor_client.headers["Origin"] = "https://evil.example"

    for tail in ("sort", "sort/reset"):
        r = await editor_client.post(f"/categories/{category.id}/{tail}", data=VALID, headers=HX)
        assert r.status_code == 403, tail

    assert await all_prefs(db) == []


def radios(html: str) -> list[str]:
    return re.findall(r'<input type="radio"[^>]* name="(\w+)" value="(\w+)"([^>]*)>', html)


def checked(html: str) -> set[tuple[str, str]]:
    return {(name, value) for name, value, rest in radios(html) if " checked" in rest}


async def test_sort_control_shows_the_current_choice(editor_client, viewer_client, board, db):
    category = await create_category(db, board, NASTY)
    await create_item(db, category, "Milk")
    path = f"/categories/{category.id}"
    default = {("sort_field", "name"), ("sort_direction", "asc"), ("sort_open_first", "true")}

    for client in (editor_client, viewer_client):
        html = (await client.get(path)).text
        assert f'aria-label="Sort {NASTY_ESCAPED}"' in html
        assert re.search(r'name="sort_field" value="name"[^>]* checked>', html)
        assert html.count('type="radio"') == 7
        assert checked(html) == default
        assert "Use my default" in html
        assert "sort-custom" not in html and 'title="Custom order"' not in html
        assert 'title="Sort"' in html

    custom = {"sort_field": "updated", "sort_direction": "desc", "sort_open_first": "false"}
    for client in (editor_client, viewer_client):
        r = await client.post(f"{path}/sort", data=custom, headers=HX)
        html = r.text
        assert "sort-custom" in html and 'title="Custom order"' in html
        assert checked(html) == {
            ("sort_field", "updated"),
            ("sort_direction", "desc"),
            ("sort_open_first", "false"),
        }
        assert "Use my default" in html
        assert checked((await client.get(path)).text) == checked(html)


async def test_fragment_and_full_board_match_with_a_custom_order(
    editor_client, board, category, db
):
    await seed_fruit(db, category)
    await editor_client.post(f"/categories/{category.id}/sort", data=VALID, headers=HX)

    fragment = section((await editor_client.get(f"/categories/{category.id}")).text, category.id)

    for path in (f"/boards/{board.id}", f"/boards/{board.id}/content"):
        assert section((await editor_client.get(path)).text, category.id) == fragment, path
    assert "sort-custom" in fragment


async def test_views_read_the_sort_row_afresh(db, board, category):
    """A session that has loaded the views once still shows a row changed meanwhile (the rows are
    written with Core statements, which leave what the session holds as it was). Each view is
    checked on its own: the one read first refreshes the row for the other."""
    await seed_fruit(db, category)
    owner = await user_named(db, "owner@example.com")
    await prefs.set_category_sort(
        db, category.id, owner, field="name", descending=True, open_first=True
    )
    for view in await both_views(db, board, category, owner):
        assert names_in(view) == ["Cherry", "Banana", "apple", "Almond"]
    # Held, so that the session keeps the row (it lets go of rows nobody refers to).
    held = await get_category_sort(db, category.id, owner)
    assert held.field == "name"

    async def change(**values):
        await db.execute(
            update(CategorySortPref)
            .where(CategorySortPref.category_id == category.id)
            .values(**values)
            .execution_options(
                synchronize_session=False
            )  # as the upsert: rows held stay as they were
        )
        await db.commit()

    await change(field="created", descending=False, open_first=False)  # no expunge_all: as is
    on_board = (await get_board_view(db, board.id, owner)).categories[0]
    assert on_board.sort == SortChoice("created", False, False, True)
    assert names_in(on_board) == ["Almond", "Cherry", "apple", "Banana"]

    await change(field="updated", descending=True, open_first=True)
    in_category, _ = await get_category_view(db, category.id, owner)
    assert in_category.sort == SortChoice("updated", True, True, True)
    assert names_in(in_category) == ["apple", "Cherry", "Banana", "Almond"]


async def test_category_view_uses_at_most_three_queries(db, board, category):
    await seed_fruit(db, category)
    owner = await user_named(db, "owner@example.com")
    db.add(CategorySortPref(user_id=owner.id, category_id=category.id, **NAME_DESCENDING))
    await db.commit()
    db.expunge_all()

    with count_queries(db) as statements:
        view, _ = await get_category_view(db, category.id, owner)

    assert len(statements) <= 3
    assert view.sort == SortChoice("name", True, True, True)
    assert names_in(view) == ["Cherry", "Banana", "apple", "Almond"]
