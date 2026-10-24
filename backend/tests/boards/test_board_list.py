"""The board list: "My boards" and "Shared with me", the "Shared" pill, the owner's e-mail on
boards of others, each board's ⋮ menu (rename, invite and archive for its owner, leave for its
other members) and the "Archived (n)" link."""

import re
from datetime import UTC, datetime

import pytest
from factories import create_board, create_user
from helpers import NASTY, NASTY_ESCAPED, signed_in

from easytodo.boards.invites import create_link_invite
from easytodo.models import BoardRole

ARCHIVED_AT = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


def section(html: str, heading_id: str) -> str:
    """The ``<section>`` labelled by the heading ``heading_id``."""
    match = re.search(rf'<section aria-labelledby="{heading_id}".*?</section>', html, re.DOTALL)
    assert match, f"no section {heading_id} on the page"
    return match[0]


def card(html: str, board_id: int) -> str:
    """The list item of one board: its link and its ⋮ menu."""
    match = re.search(
        rf'<li class="card[^"]*">\s*<a href="/boards/{board_id}".*?</li>', html, re.DOTALL
    )
    assert match, f"no card for board {board_id} on the page"
    return match[0]


def board_ids(html: str) -> list[int]:
    return [int(board_id) for board_id in re.findall(r'href="/boards/(\d+)"', html)]


async def list_page(client_factory, email: str = "dave@example.com") -> str:
    client = await signed_in(client_factory, email)
    r = await client.get("/boards")
    assert r.status_code == 200
    return r.text


async def test_boards_are_split_into_sections_with_owner_and_role(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    groceries = await create_board(db, dave, "Groceries")
    hardware = await create_board(db, eve, "Hardware store", {dave: BoardRole.VIEWER})
    club = await create_board(db, eve, "Apple club", {dave: BoardRole.EDITOR})
    await create_board(db, eve, "Eve private plans")

    html = await list_page(client_factory)

    assert board_ids(section(html, "my-boards")) == [groceries.id]
    assert board_ids(section(html, "shared-boards")) == [club.id, hardware.id]
    assert '<h2 id="my-boards"' in html and ">My boards</h2>" in html
    assert '<h2 id="shared-boards"' in html and ">Shared with me</h2>" in html
    assert '<span class="break-all">Owner: eve@example.com</span>' in card(html, hardware.id)
    assert ">Viewer</span>" in card(html, hardware.id)
    assert ">Editor</span>" in card(html, club.id)
    assert "Owner:" not in card(html, groceries.id)
    assert ">Owner</span>" not in html  # the owner's boards show no role
    assert "Eve private plans" not in html


async def test_shared_pill_only_on_own_boards_with_other_members(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    alone = await create_board(db, dave, "Alone")
    with_editor = await create_board(db, dave, "With editor", {eve: BoardRole.EDITOR})
    only_invited = await create_board(db, dave, "Only invited")
    await create_link_invite(db, only_invited.id, dave, BoardRole.EDITOR)  # not a member yet
    theirs = await create_board(db, eve, "Theirs", {dave: BoardRole.EDITOR})

    html = await list_page(client_factory)

    assert ">Shared</span>" in card(html, with_editor.id)
    for board in (alone, only_invited, theirs):
        assert ">Shared</span>" not in card(html, board.id), board.name
    assert html.count(">Shared</span>") == 1


async def test_empty_sections_say_so(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    nobody = "Nobody has shared a board with you yet."
    none_yet = "You have no boards yet."

    html = await list_page(client_factory)
    assert none_yet in section(html, "my-boards") and nobody in section(html, "shared-boards")
    assert board_ids(html) == []

    await create_board(db, dave, "Groceries")
    html = await list_page(client_factory)
    assert none_yet not in html and nobody in section(html, "shared-boards")

    await create_board(db, eve, "Club", {dave: BoardRole.VIEWER})
    html = await list_page(client_factory)
    assert none_yet not in html and nobody not in html

    html = await list_page(client_factory, "eve@example.com")
    assert none_yet not in html and nobody in html  # Eve owns Club, and nothing is shared with her


async def test_a_user_with_only_shared_boards_has_an_empty_first_section(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    await create_board(db, eve, "Club", {dave: BoardRole.VIEWER})

    html = await list_page(client_factory)

    assert "You have no boards yet." in section(html, "my-boards")
    assert "Nobody has shared a board with you yet." not in html


async def test_owner_menu_renames_invites_and_archives(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    groceries = await create_board(db, dave, "Groceries")
    board_id = groceries.id

    menu = card(await list_page(client_factory), board_id)

    assert '<details class="relative shrink-0">' in menu
    assert re.search(
        r'<summary class="btn-icon[^"]*" aria-label="Actions for Groceries">⋮</summary>', menu
    )
    assert '<div class="menu">' in menu
    assert f'<form hx-patch="/boards/{board_id}"' in menu
    assert f'<label for="board-{board_id}-name"' in menu and ">Board name</label>" in menu
    assert (
        f'<input id="board-{board_id}-name" name="name" value="Groceries" required maxlength="80"'
        in menu
    )
    assert ">Rename</button>" in menu
    assert re.search(rf'<a href="/boards/{board_id}/members"[^>]*>Invite</a>', menu)
    assert f'hx-post="/boards/{board_id}/archive"' in menu
    assert 'hx-confirm="Archive “Groceries”? Its members lose access until you restore it."' in menu
    assert re.search(r"<button[^>]*>Archive</button>", menu)
    assert "/leave" not in menu
    # The link and the menu are siblings: a menu inside a link would be a link in a link.
    assert not re.search(r"<a [^>]*>(?:(?!</a>).)*<details", menu, re.DOTALL)


@pytest.mark.parametrize("role", [BoardRole.EDITOR, BoardRole.VIEWER])
async def test_members_menu_has_only_leave(client_factory, db, role):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    club = await create_board(db, eve, "Club", {dave: role})

    menu = card(await list_page(client_factory), club.id)

    assert re.search(r'<summary class="btn-icon[^"]*" aria-label="Actions for Club">', menu)
    assert f'hx-post="/boards/{club.id}/leave"' in menu
    assert 'hx-confirm="Leave “Club”?"' in menu
    assert re.search(r"<button[^>]*>Leave board</button>", menu)
    for owner_only in ("hx-patch=", "/archive", "/members", "Rename", "Invite", 'name="name"'):
        assert owner_only not in menu


async def test_archived_link_only_when_there_are_archived_boards(client_factory, db):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    client = await signed_in(client_factory, "dave@example.com")
    await create_board(db, dave, "Groceries")
    await create_board(db, eve, "Club", {dave: BoardRole.EDITOR})

    html = (await client.get("/boards")).text
    assert 'href="/boards/archived"' not in html and "Archived (" not in html

    # Another owner's archived board, with Dave a member, is not Dave's to restore.
    await create_board(db, eve, "Old club", {dave: BoardRole.EDITOR}, archived_at=ARCHIVED_AT)
    html = (await client.get("/boards")).text
    assert 'href="/boards/archived"' not in html

    await create_board(db, dave, "Old list", archived_at=ARCHIVED_AT)
    html = (await client.get("/boards")).text
    assert re.search(
        r'<a href="/boards/archived" class="link inline-flex min-h-11 items-center">'
        r"Archived \(1\)</a>",
        html,
    )
    assert "Old list" not in html and "Old club" not in html  # archived boards are not listed

    failed = await client.post("/boards", data={"name": ""})  # 422 re-renders the list
    assert failed.status_code == 422
    assert "Archived (1)" in failed.text
    assert failed.text.index("Archived (1)") < failed.text.index("New board</label>")


async def test_board_list_escapes_names_and_owner_emails(client_factory, db):  # Review Focus 3
    dave = await create_user(db, "dave@example.com")
    odd = await create_user(db, "o'reilly<b>@example.com")
    await create_board(db, dave, NASTY)
    await create_board(db, odd, NASTY, {dave: BoardRole.VIEWER})

    html = await list_page(client_factory)

    assert "<b>" not in html and "<script>" not in html
    assert f'aria-label="Actions for {NASTY_ESCAPED}"' in html
    assert f'value="{NASTY_ESCAPED}"' in html
    assert f"Archive “{NASTY_ESCAPED}”?" in html
    assert f"Leave “{NASTY_ESCAPED}”?" in html
    assert "Owner: o&#39;reilly&lt;b&gt;@example.com" in html
