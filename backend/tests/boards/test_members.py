"""Board members: the members page, changing a member's role, removing a member and leaving."""

import re

import pytest
from factories import create_board, create_user
from helpers import LINKS_STILL_WORK, signed_in, toast_message
from sqlalchemy import select

from easytodo.boards.invites import create_email_invite, create_link_invite
from easytodo.mail.mailer import ConsoleMailer
from easytodo.models import Board, BoardInvite, BoardMember, BoardRole
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_HEADER

HX = {"HX-Request": "true"}
NO_PERMISSION = "You don't have permission to do that."
OWNER_ROLE = "The owner's role can't be changed."
OWNER_LEAVES = "The owner can't leave. Archive the board instead."


async def members_of(db, board_id: int) -> dict[int, BoardRole]:
    rows = await db.execute(
        select(BoardMember.user_id, BoardMember.role).where(BoardMember.board_id == board_id)
    )
    return dict(rows.all())


@pytest.fixture
async def team(db):
    """owner@ owns "Groceries"; eve@ is an editor of it, frank@ a viewer."""
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com")
    frank = await create_user(db, "frank@example.com")
    board = await create_board(
        db, owner, "Groceries", members={eve: BoardRole.EDITOR, frank: BoardRole.VIEWER}
    )
    return board, owner, eve, frank


async def test_members_page_lists_members_with_roles(client_factory, db, team):
    board, owner, eve, frank = team
    path = f"/boards/{board.id}/members"

    page = (await (await signed_in(client_factory, "owner@example.com")).get(path)).text
    assert "<title>Invite and members · Groceries · EasyToDo</title>" in page
    assert re.search(r"<h1[^>]*>Invite and members</h1>\s*<p[^>]*>Groceries</p>", page)
    assert re.search(r'<a href="/boards"[^>]*>Back to boards</a>', page)
    assert f'href="/boards/{board.id}"' not in page
    assert page.index("owner@example.com") < page.index("eve@example.com")
    assert page.index("eve@example.com") < page.index("frank@example.com")
    assert f'hx-patch="/boards/{board.id}/members/{eve.id}"' in page
    assert f'hx-delete="/boards/{board.id}/members/{frank.id}"' in page
    assert f"/members/{owner.id}" not in page  # no controls on the owner's own row
    assert f'action="/boards/{board.id}/invites"' in page
    assert f'action="/boards/{board.id}/leave"' not in page

    page = (await (await signed_in(client_factory, "frank@example.com")).get(path)).text
    for email in ("owner@example.com", "eve@example.com", "frank@example.com"):
        assert email in page
    assert "Owner" in page and "Editor" in page and "Viewer" in page
    for control in ("hx-patch=", "hx-delete=", f'action="/boards/{board.id}/invites"'):
        assert control not in page
    assert f'action="/boards/{board.id}/leave"' in page

    # The way to this page is the owner's ⋮ menu on the board list; a member has no link to it
    # (they leave from their own ⋮ menu).
    for email, linked in (("owner@example.com", True), ("frank@example.com", False)):
        client = await signed_in(client_factory, email)
        assert (f'href="{path}"' in (await client.get("/boards")).text) is linked
        assert f'href="{path}"' not in (await client.get(f"/boards/{board.id}")).text


async def test_non_members_get_404_for_the_members_page(client_factory, db, team):
    board, *_ = team
    await create_user(db, "stranger@example.com")
    stranger = await signed_in(client_factory, "stranger@example.com")
    for board_id in (board.id, 999999, 2**63):
        r = await stranger.get(f"/boards/{board_id}/members")
        assert r.status_code == 404
        assert "Board not found." in r.text and "eve@example.com" not in r.text


async def test_owner_changes_a_members_role(client_factory, db, team, sharing_events):
    board, owner, eve, frank = team
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.patch(
        f"/boards/{board.id}/members/{eve.id}", data={"role": "viewer"}, headers=HX
    )

    assert r.status_code == 200
    assert 'id="sharing"' in r.text  # the members page, for hx-select
    assert await members_of(db, board.id) == {
        owner.id: BoardRole.OWNER,
        eve.id: BoardRole.VIEWER,
        frank.id: BoardRole.VIEWER,
    }
    # Carries the member, so that member's own page can reload with its new controls.
    assert sharing_events == [
        BoardEvent(EventType.BOARD_UPDATED, board_id=board.id, user_id=eve.id),
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=eve.id),  # the bell on their devices
    ]
    eve_client = await signed_in(client_factory, "eve@example.com")
    r = await eve_client.post(f"/boards/{board.id}/categories", data={"name": "Dairy"}, headers=HX)
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["admin", ""])
async def test_role_must_be_editor_or_viewer(client_factory, db, team, role):
    board, _, eve, _ = team
    owner_client = await signed_in(client_factory, "owner@example.com")
    r = await owner_client.patch(f"/boards/{board.id}/members/{eve.id}", data={"role": role})
    assert r.status_code == 422
    assert "Choose editor or viewer." in r.text
    assert (await members_of(db, board.id))[eve.id] == BoardRole.EDITOR


async def test_removed_member_loses_access(client_factory, db, team, sharing_events):
    board, owner, eve, frank = team
    owner_client = await signed_in(client_factory, "owner@example.com")
    eve_client = await signed_in(client_factory, "eve@example.com")
    assert (await eve_client.get(f"/boards/{board.id}")).status_code == 200

    r = await owner_client.delete(f"/boards/{board.id}/members/{eve.id}", headers=HX)

    assert r.status_code == 200
    assert "eve@example.com" not in r.text
    assert await members_of(db, board.id) == {owner.id: BoardRole.OWNER, frank.id: BoardRole.VIEWER}
    assert sharing_events == [
        BoardEvent(EventType.MEMBER_REMOVED, board_id=board.id, user_id=eve.id),
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=eve.id),
    ]
    for path in (f"/boards/{board.id}", f"/boards/{board.id}/members"):
        r = await eve_client.get(path)
        assert r.status_code == 404
        assert "Board not found." in r.text


async def test_remove_warns_that_invite_links_still_work(client_factory, db, team, settings):
    """An invite link works for anyone who has it, the person removed included: say so where the
    owner confirms the removal. An e-mail invite is for one address and is not mentioned."""
    board, owner, *_ = team
    path = f"/boards/{board.id}/members"
    owner_client = await signed_in(client_factory, "owner@example.com")
    plain = 'hx-confirm="Remove eve@example.com from this board?"'
    warned = f'hx-confirm="Remove eve@example.com from this board? {LINKS_STILL_WORK}"'
    assert plain in (await owner_client.get(path)).text

    await create_email_invite(
        db, board.id, owner, "friend@example.com", BoardRole.EDITOR, ConsoleMailer(), settings
    )
    page = (await owner_client.get(path)).text
    assert plain in page and LINKS_STILL_WORK not in page

    _, link = await create_link_invite(db, board.id, owner, BoardRole.EDITOR)
    page = (await owner_client.get(path)).text
    assert warned in page and plain not in page
    assert page.count(LINKS_STILL_WORK) == 2  # every member who can be removed: eve and frank

    await owner_client.delete(f"/boards/{board.id}/invites/{link.id}")
    assert plain in (await owner_client.get(path)).text  # a revoked link rejoins nobody


async def test_member_leaves_board(client_factory, db, team, sharing_events):
    board, owner, eve, frank = team
    eve_client = await signed_in(client_factory, "eve@example.com")
    frank_client = await signed_in(client_factory, "frank@example.com")

    r = await eve_client.post(f"/boards/{board.id}/leave", headers=HX)
    assert r.status_code == 200
    assert r.headers["hx-redirect"] == "/boards"

    r = await frank_client.post(f"/boards/{board.id}/leave")
    assert r.status_code == 303
    assert r.headers["location"] == "/boards"

    assert await members_of(db, board.id) == {owner.id: BoardRole.OWNER}
    assert sharing_events == [  # each leaving tells the owner
        BoardEvent(EventType.MEMBER_REMOVED, board_id=board.id, user_id=eve.id),
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=owner.id),
        BoardEvent(EventType.MEMBER_REMOVED, board_id=board.id, user_id=frank.id),
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=owner.id),
    ]
    assert (await eve_client.get(f"/boards/{board.id}")).status_code == 404
    assert "Groceries" not in (await eve_client.get("/boards")).text


async def test_owner_cannot_leave_or_be_demoted(client_factory, db, team, sharing_events):
    board, owner, eve, frank = team
    owner_client = await signed_in(client_factory, "owner@example.com")
    members = f"/boards/{board.id}/members"

    r = await owner_client.post(f"/boards/{board.id}/leave", headers=HX)
    assert r.status_code == 409
    assert toast_message(r) == OWNER_LEAVES

    for target in (owner, eve):  # the owner stays the owner; nobody else becomes one
        r = await owner_client.patch(f"{members}/{target.id}", data={"role": "owner"}, headers=HX)
        assert r.status_code == 403
        assert toast_message(r) == OWNER_ROLE
    r = await owner_client.patch(f"{members}/{owner.id}", data={"role": "editor"}, headers=HX)
    assert r.status_code == 403
    assert toast_message(r) == OWNER_ROLE

    r = await owner_client.delete(f"{members}/{owner.id}", headers=HX)
    assert r.status_code == 409
    assert toast_message(r) == OWNER_LEAVES

    assert await members_of(db, board.id) == {
        owner.id: BoardRole.OWNER,
        eve.id: BoardRole.EDITOR,
        frank.id: BoardRole.VIEWER,
    }
    assert await db.scalar(select(Board.owner_id)) == owner.id
    assert sharing_events == []


async def test_only_the_owner_manages_members(client_factory, db, team):
    board, owner, eve, frank = team
    stranger = await create_user(db, "stranger@example.com")
    await create_board(db, stranger, "Stranger's board")

    for email in ("eve@example.com", "frank@example.com"):
        member = await signed_in(client_factory, email)
        for target in (owner, eve, frank):
            path = f"/boards/{board.id}/members/{target.id}"
            r = await member.patch(path, data={"role": "viewer"}, headers=HX)
            assert r.status_code == 403
            assert toast_message(r) == NO_PERMISSION
            assert (await member.delete(path, headers=HX)).status_code == 403

    outsider = await signed_in(client_factory, "stranger@example.com")
    for target in (owner, eve, frank, stranger):
        r = await outsider.patch(f"/boards/{board.id}/members/{target.id}", data={"role": "editor"})
        assert r.status_code == 404
        r = await outsider.delete(f"/boards/{board.id}/members/{target.id}")
        assert r.status_code == 404
    r = await outsider.post(f"/boards/{board.id}/leave")
    assert r.status_code == 404

    assert await members_of(db, board.id) == {
        owner.id: BoardRole.OWNER,
        eve.id: BoardRole.EDITOR,
        frank.id: BoardRole.VIEWER,
    }


async def test_unknown_member_is_404(client_factory, db, team):
    board, *_ = team
    stranger = await create_user(db, "stranger@example.com")
    owner_client = await signed_in(client_factory, "owner@example.com")
    for user_id in (stranger.id, 999999, 0, 2**63):
        path = f"/boards/{board.id}/members/{user_id}"
        r = await owner_client.patch(path, data={"role": "viewer"})
        assert r.status_code == 404, user_id
        assert "Member not found." in r.text
        assert (await owner_client.delete(path)).status_code == 404


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("POST", "/boards/{board}/invites", {"role": "editor", "email": ""}),
        ("DELETE", "/boards/{board}/invites/{invite}", None),
        ("PATCH", "/boards/{board}/members/{eve}", {"role": "viewer"}),
        ("DELETE", "/boards/{board}/members/{eve}", None),
        ("POST", "/boards/{board}/leave", None),
        ("POST", "/invite/{token}/accept", None),
    ],
)
async def test_sharing_writes_require_the_csrf_token(client_factory, db, team, method, path, data):
    board, owner, eve, _ = team
    token, invite = await create_link_invite(db, board.id, owner, BoardRole.EDITOR)
    as_eve = path.endswith("/leave") or path.startswith("/invite/")
    client = await signed_in(client_factory, "eve@example.com" if as_eve else "owner@example.com")
    del client.headers[CSRF_HEADER]
    url = path.format(board=board.id, invite=invite.id, eve=eve.id, token=token)

    r = await client.request(method, url, data=data)

    assert r.status_code == 403
    assert (await members_of(db, board.id))[eve.id] == BoardRole.EDITOR
    assert await db.scalar(select(BoardInvite.revoked_at)) is None
    assert len(list(await db.scalars(select(BoardInvite.id)))) == 1


async def test_sharing_pages_render_names_escaped(client_factory, db):  # Review Focus 1
    nasty = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
    escaped = "O&#39;Reilly &lt;b&gt;&#34;x&#34;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;"
    owner = await create_user(db, "owner@example.com")
    await create_user(db, "bob@example.com")
    board = await create_board(db, owner, nasty)
    owner_client = await signed_in(client_factory, "owner@example.com")
    r = await owner_client.post(f"/boards/{board.id}/invites", data={"role": "viewer"})
    token = r.text.split("/invite/", 1)[1].split('"', 1)[0]
    bob_client = await signed_in(client_factory, "bob@example.com")

    for client, path in (
        (owner_client, f"/boards/{board.id}/members"),
        (bob_client, f"/invite/{token}"),
    ):
        html = (await client.get(path)).text
        assert "<script>alert(1)</script>" not in html and "<b>" not in html
        assert escaped in html
