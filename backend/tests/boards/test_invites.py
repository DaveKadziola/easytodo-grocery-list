"""Board invites: a link (reusable until it expires or is revoked), an e-mail (single use, for that
address only), and the sign-in or sign-up round trip from an invite link."""

import logging
import re
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from factories import DEFAULT_PASSWORD, create_board, create_user
from helpers import login, signed_in, toast_message
from sqlalchemy import func, select, update

from easytodo.boards.invite_routes import EMAIL_INVITE_LIMIT
from easytodo.boards.invites import INVITE_TTL, create_email_invite, create_link_invite
from easytodo.mail.mailer import ConsoleMailer
from easytodo.models import BoardInvite, BoardMember, BoardRole
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_COOKIE
from easytodo.security.tokens import hash_token

INVALID = "This invite is no longer valid."
OTHER_ADDRESS = "This invite was sent to a different email address."
NO_PERMISSION = "You don't have permission to do that."
TOO_MANY = "Too many requests. Try again later."
HX = {"HX-Request": "true"}
LINK = re.compile(r"http://testserver/invite/([A-Za-z0-9_-]+)")


async def invite_by_link(owner_client, board_id: int, role: str = "editor") -> str:
    """Create a link invite on the members page; returns the token from the link it shows."""
    r = await owner_client.post(f"/boards/{board_id}/invites", data={"role": role, "email": ""})
    assert r.status_code == 201, r.text
    return LINK.search(r.text)[1]


async def invite_by_email(owner_client, board_id: int, email: str, role: str = "editor"):
    r = await owner_client.post(f"/boards/{board_id}/invites", data={"role": role, "email": email})
    assert r.status_code == 201, r.text
    return r


async def role_of(db, board_id: int, user_id: int) -> BoardRole | None:
    return await db.scalar(
        select(BoardMember.role).where(
            BoardMember.board_id == board_id, BoardMember.user_id == user_id
        )
    )


async def count_invites(db) -> int:
    return await db.scalar(select(func.count()).select_from(BoardInvite))


async def test_link_invite_grants_role(client_factory, db, sharing_events):
    owner = await create_user(db, "owner@example.com")
    bob = await create_user(db, "bob@example.com")
    carol = await create_user(db, "carol@example.com")
    board = await create_board(db, owner, "Groceries")
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.post(f"/boards/{board.id}/invites", data={"role": "editor"})

    assert r.status_code == 201
    token = LINK.search(r.text)[1]
    assert re.search(
        r'<input id="invite-link" value="http://testserver/invite/[^"]+" readonly', r.text
    )
    assert 'data-copy="invite-link"' in r.text and ">Copy</button>" in r.text
    # The link is shown once: the members page does not show it again.
    assert token not in (await owner_client.get(f"/boards/{board.id}/members")).text

    for user in (bob, carol):  # anyone who has the link can use it until it expires
        member = await signed_in(client_factory, user.email)
        page = await member.get(f"/invite/{token}")
        assert page.status_code == 200
        assert "Groceries" in page.text and "owner@example.com" in page.text
        assert f'action="/invite/{token}/accept"' in page.text

        r = await member.post(f"/invite/{token}/accept")

        assert r.status_code == 303
        assert r.headers["location"] == f"/boards/{board.id}"
        assert await role_of(db, board.id, user.id) == BoardRole.EDITOR
        assert (await member.get(f"/boards/{board.id}")).status_code == 200

    assert sharing_events == [BoardEvent(EventType.BOARD_UPDATED, board_id=board.id)] * 2


async def test_only_owner_can_invite(editor_client, board, client_factory, db):
    viewer = await create_user(db, "viewer@example.com")
    db.add(BoardMember(board_id=board.id, user_id=viewer.id, role=BoardRole.VIEWER))
    await db.commit()
    await create_user(db, "stranger@example.com")
    viewer_client = await signed_in(client_factory, "viewer@example.com")
    stranger_client = await signed_in(client_factory, "stranger@example.com")
    path = f"/boards/{board.id}/invites"

    for data in ({"role": "editor", "email": ""}, {"role": "viewer", "email": "x@example.com"}):
        for member in (editor_client, viewer_client):
            r = await member.post(path, data=data, headers=HX)
            assert r.status_code == 403
            assert toast_message(r) == NO_PERMISSION
        r = await stranger_client.post(path, data=data)
        assert r.status_code == 404
        assert "Board not found." in r.text

    page = await editor_client.get(f"/boards/{board.id}/members")
    assert page.status_code == 200
    assert f'action="{path}"' not in page.text  # no invite form for an editor
    assert await count_invites(db) == 0


@pytest.mark.parametrize("role", ["owner", "admin", ""])
async def test_invite_role_must_be_editor_or_viewer(client, board, db, role):
    await login(client, "owner@example.com")
    for email in ("", "friend@example.com"):
        r = await client.post(f"/boards/{board.id}/invites", data={"role": role, "email": email})
        assert r.status_code == 422
        assert "Choose editor or viewer." in r.text
    assert await count_invites(db) == 0


async def test_expired_or_revoked_invite_is_invalid(client_factory, db):
    owner = await create_user(db, "owner@example.com")
    bob = await create_user(db, "bob@example.com")
    board = await create_board(db, owner, "Groceries")
    owner_client = await signed_in(client_factory, "owner@example.com")
    expired = await invite_by_link(owner_client, board.id)
    revoked = await invite_by_link(owner_client, board.id)
    await db.execute(
        update(BoardInvite)
        .where(BoardInvite.token_hash == hash_token(expired))
        .values(expires_at=func.now() - timedelta(seconds=1))
    )
    await db.commit()
    revoked_id = await db.scalar(
        select(BoardInvite.id).where(BoardInvite.token_hash == hash_token(revoked))
    )
    page = await owner_client.get(f"/boards/{board.id}/members")
    assert f'hx-delete="/boards/{board.id}/invites/{revoked_id}"' in page.text

    r = await owner_client.delete(f"/boards/{board.id}/invites/{revoked_id}")

    assert r.status_code == 200
    assert f"/invites/{revoked_id}" not in r.text  # no longer pending
    bob_client = await signed_in(client_factory, "bob@example.com")
    for token in (expired, revoked, "no-such-token"):
        for method, path in (("GET", f"/invite/{token}"), ("POST", f"/invite/{token}/accept")):
            r = await bob_client.request(method, path)
            assert r.status_code == 404, (token, method)
            assert INVALID in r.text
    assert await role_of(db, board.id, bob.id) is None


async def test_others_cannot_revoke_an_invite(client_factory, db, editor_client, board, owner):
    _, invite = await create_link_invite(db, board.id, owner, BoardRole.VIEWER)
    stranger = await create_user(db, "stranger@example.com")
    own_board = await create_board(db, stranger, "Stranger's board")
    stranger_client = await signed_in(client_factory, "stranger@example.com")

    r = await editor_client.delete(f"/boards/{board.id}/invites/{invite.id}")
    assert r.status_code == 403
    for board_id in (board.id, own_board.id):
        path = f"/boards/{board_id}/invites/{invite.id}"
        r = await stranger_client.delete(path)
        assert r.status_code == 404
    r = await stranger_client.delete(f"/boards/{board.id}/invites/{2**63}")
    assert r.status_code == 404

    assert await db.scalar(select(BoardInvite.revoked_at)) is None


async def test_email_invite_sends_mail_with_link(client, board, mail_outbox, db):
    await login(client, "owner@example.com")

    r = await invite_by_email(client, board.id, "friend@example.com", role="viewer")

    assert "Invite sent to friend@example.com." in r.text
    assert "http://testserver/invite/" not in r.text  # the link goes to the mailbox only
    assert len(mail_outbox) == 1
    mail = mail_outbox[0]
    assert mail.to == "friend@example.com"
    assert mail.subject == "owner@example.com invited you to a board on EasyToDo"
    assert "http://testserver/invite/" in mail.body
    assert '"Groceries"' in mail.body and "as a viewer" in mail.body
    token = LINK.search(mail.body)[1]
    invite = await db.scalar(select(BoardInvite))
    assert (invite.token_hash, invite.email, invite.role) == (
        hash_token(token),
        "friend@example.com",
        BoardRole.VIEWER,
    )
    page = await client.get(f"/boards/{board.id}/members")
    assert "friend@example.com" in page.text  # listed as pending


async def test_email_invite_matches_case_insensitively(client_factory, db, mail_outbox):
    # Review Focus 5
    owner = await create_user(db, "owner@example.com")
    friend = await create_user(db, "friend@example.com")
    board = await create_board(db, owner, "Groceries")
    owner_client = await signed_in(client_factory, "owner@example.com")

    await invite_by_email(owner_client, board.id, " Friend@Example.com ")

    assert mail_outbox[0].to == "friend@example.com"
    assert await db.scalar(select(BoardInvite.email)) == "friend@example.com"
    friend_client = await signed_in(client_factory, " FRIEND@example.COM")
    r = await friend_client.post(f"/invite/{LINK.search(mail_outbox[0].body)[1]}/accept")
    assert r.status_code == 303
    assert r.headers["location"] == f"/boards/{board.id}"
    assert await role_of(db, board.id, friend.id) == BoardRole.EDITOR


async def test_email_invite_rejects_other_account(client_factory, db, mail_outbox):
    owner = await create_user(db, "owner@example.com")
    mallory = await create_user(db, "mallory@example.com")
    board = await create_board(db, owner, "Groceries")
    owner_client = await signed_in(client_factory, "owner@example.com")
    await invite_by_email(owner_client, board.id, "friend@example.com")
    token = LINK.search(mail_outbox[0].body)[1]
    mallory_client = await signed_in(client_factory, "mallory@example.com")

    r = await mallory_client.post(f"/invite/{token}/accept")

    assert r.status_code == 403
    assert OTHER_ADDRESS in r.text
    assert await role_of(db, board.id, mallory.id) is None
    assert await db.scalar(select(BoardInvite.accepted_at)) is None  # still usable by friend


async def test_email_invite_is_single_use(client_factory, db, mail_outbox):
    owner = await create_user(db, "owner@example.com")
    friend = await create_user(db, "friend@example.com")
    board = await create_board(db, owner, "Groceries")
    owner_client = await signed_in(client_factory, "owner@example.com")
    await invite_by_email(owner_client, board.id, "friend@example.com")
    token = LINK.search(mail_outbox[0].body)[1]
    friend_client = await signed_in(client_factory, "friend@example.com")

    assert (await friend_client.post(f"/invite/{token}/accept")).status_code == 303
    assert await db.scalar(select(BoardInvite.accepted_at)) is not None
    assert (await friend_client.post(f"/boards/{board.id}/leave")).status_code == 303

    for method, path in (("GET", f"/invite/{token}"), ("POST", f"/invite/{token}/accept")):
        r = await friend_client.request(method, path)
        assert r.status_code == 404
        assert INVALID in r.text
    assert await role_of(db, board.id, friend.id) is None
    page = await owner_client.get(f"/boards/{board.id}/members")
    assert "friend@example.com" not in page.text  # no longer pending


async def test_email_invites_are_rate_limited_per_inviter(client_factory, db, mail_outbox):
    owner = await create_user(db, "owner@example.com")
    other = await create_user(db, "other@example.com")
    board = await create_board(db, owner, "Groceries")
    second_board = await create_board(db, owner, "Hardware")
    other_board = await create_board(db, other, "Garden")
    owner_client = await signed_in(client_factory, "owner@example.com")
    for n in range(EMAIL_INVITE_LIMIT):
        await invite_by_email(owner_client, board.id, f"friend{n}@example.com")
    over_limit = {"role": "editor", "email": "one-more@example.com"}

    r = await owner_client.post(f"/boards/{board.id}/invites", data=over_limit, headers=HX)

    assert r.status_code == 429
    assert toast_message(r) == TOO_MANY
    r = await owner_client.post(f"/boards/{second_board.id}/invites", data=over_limit)
    assert r.status_code == 429  # the limit is the inviter's, on every board
    assert TOO_MANY in r.text
    assert len(mail_outbox) == EMAIL_INVITE_LIMIT  # the refused requests sent no mail
    assert await count_invites(db) == EMAIL_INVITE_LIMIT  # and stored no invite
    await invite_by_link(owner_client, board.id)  # a link invite sends no mail: not limited
    # Another user (the test clients share one IP) has a limit of their own.
    other_client = await signed_in(client_factory, "other@example.com")
    await invite_by_email(other_client, other_board.id, "friend@example.com")
    assert len(mail_outbox) == EMAIL_INVITE_LIMIT + 1


async def test_email_invite_survives_a_mail_server_failure(client_factory, db, caplog):
    owner = await create_user(db, "owner@example.com")
    board = await create_board(db, owner, "Groceries")
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    smtp_client = await client_factory(
        mail_backend="smtp", smtp_host="127.0.0.1", smtp_port=port, smtp_starttls=False
    )
    await login(smtp_client, "owner@example.com")

    with caplog.at_level(logging.ERROR, logger="easytodo.mail"):
        r = await invite_by_email(smtp_client, board.id, "friend@example.com")

    assert "Invite sent to friend@example.com." in r.text
    assert await db.scalar(select(BoardInvite.email)) == "friend@example.com"
    assert [rec.levelno for rec in caplog.records if "friend@example.com" in rec.getMessage()] == [
        logging.ERROR
    ]


async def test_accepting_when_already_member_keeps_role(client_factory, db, sharing_events):
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com")
    board = await create_board(db, owner, "Groceries", members={eve: BoardRole.EDITOR})
    owner_client = await signed_in(client_factory, "owner@example.com")
    token = await invite_by_link(owner_client, board.id, role="viewer")
    eve_client = await signed_in(client_factory, "eve@example.com")

    for client, user, role in (
        (eve_client, eve, BoardRole.EDITOR),
        (owner_client, owner, BoardRole.OWNER),
    ):
        r = await client.post(f"/invite/{token}/accept")
        assert r.status_code == 303
        assert r.headers["location"] == f"/boards/{board.id}"
        assert await role_of(db, board.id, user.id) == role

    assert sharing_events == []  # nothing changed


async def test_email_invite_to_a_member_is_used_up_and_keeps_the_role(
    client_factory, db, mail_outbox, sharing_events
):
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com")
    board = await create_board(db, owner, "Groceries", members={eve: BoardRole.EDITOR})
    owner_client = await signed_in(client_factory, "owner@example.com")
    await invite_by_email(owner_client, board.id, "eve@example.com", role="viewer")
    token = LINK.search(mail_outbox[0].body)[1]
    eve_client = await signed_in(client_factory, "eve@example.com")

    r = await eve_client.post(f"/invite/{token}/accept")

    assert r.status_code == 303
    assert await role_of(db, board.id, eve.id) == BoardRole.EDITOR
    assert await db.scalar(select(BoardInvite.accepted_at)) is not None
    assert (await eve_client.get(f"/invite/{token}")).status_code == 404
    assert sharing_events == []


async def test_anonymous_invite_visit_redirects_to_login_with_next(client, invite_token):
    r = await client.get(f"/invite/{invite_token}")

    assert r.status_code == 303
    location = urlsplit(r.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {"next": [f"/invite/{invite_token}"]}
    page = await client.get(r.headers["location"])
    assert (
        f'<a href="/register?next=/invite/{invite_token}" class="link">Create an account</a>'
        in page.text
    )


async def test_registration_from_an_invite_link_returns_to_it(client, db, invite_token):
    next_path = f"/invite/{invite_token}"
    page = await client.get("/register", params={"next": next_path})
    assert 'action="/register"' not in page.text  # the form posts back to /register?next=...
    assert f'href="/login?next={next_path}"' in page.text

    r = await client.post(
        "/register",
        params={"next": next_path},
        data={
            "email": "newbie@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )

    assert r.status_code == 303
    assert r.headers["location"] == next_path
    page = await client.get(next_path)
    assert page.status_code == 200 and "Groceries" in page.text


@pytest.mark.parametrize("next_path", ["https://evil.example", "//evil.example", "/\\evil.example"])
async def test_sign_up_drops_an_external_next(client, next_path):
    for path in ("/login", "/register"):
        page = await client.get(path, params={"next": next_path})
        assert "evil.example" not in page.text

    r = await client.post(
        "/register",
        params={"next": next_path},
        data={
            "email": "newbie@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_invite_token_stored_hashed(db, settings, board, owner):
    mailer = ConsoleMailer()

    link_token, link_invite = await create_link_invite(db, board.id, owner, BoardRole.EDITOR)
    email_invite = await create_email_invite(
        db, board.id, owner, "friend@example.com", BoardRole.VIEWER, mailer, settings
    )

    email_token = LINK.search(mailer.outbox[0].body)[1]
    stored = (await db.execute(select(BoardInvite.id, BoardInvite.token_hash))).all()
    assert dict(stored) == {
        link_invite.id: hash_token(link_token),
        email_invite.id: hash_token(email_token),
    }
    for _, token_hash in stored:
        assert link_token not in token_hash and email_token not in token_hash
    assert link_token != email_token
    expected = datetime.now(UTC) + INVITE_TTL
    for expires_at in await db.scalars(select(BoardInvite.expires_at)):
        assert abs(expires_at - expected) < timedelta(minutes=1)
