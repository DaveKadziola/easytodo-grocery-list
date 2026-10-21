"""Signing up from an invite link while registration is closed (EASYTODO_REGISTRATION_OPEN=false):
``/register`` opens only for a ``next`` that is a valid invite's page, an e-mail invite only for
its own address, and the new account then joins on the invite page like anyone else."""

from datetime import timedelta

import httpx
import pytest
from factories import DEFAULT_PASSWORD, create_board, create_user
from sqlalchemy import func, select, update

from easytodo.boards.invites import (
    INVITE_INVALID,
    INVITE_OTHER_EMAIL,
    create_email_invite,
    create_link_invite,
    revoke_invite,
)
from easytodo.mail.mailer import ConsoleMailer
from easytodo.models import Board, BoardInvite, BoardMember, BoardRole, User
from easytodo.security.csrf import CSRF_COOKIE
from easytodo.security.tokens import hash_token

NEWBIE = "newbie@example.com"


@pytest.fixture
async def owner(db) -> User:
    return await create_user(db, "owner@example.com")


@pytest.fixture
async def board(db, owner) -> Board:
    return await create_board(db, owner, "Groceries")


@pytest.fixture
async def closed(client_factory) -> httpx.AsyncClient:
    """A client of an app with registration closed."""
    return await client_factory(registration_open=False)


async def link_invite(db, board: Board, owner: User) -> tuple[str, BoardInvite]:
    """A link invite to ``board`` as a viewer: its raw token and its row."""
    return await create_link_invite(db, board.id, owner, BoardRole.VIEWER)


async def email_invite(db, settings, board: Board, owner: User, email: str) -> str:
    """The raw token of an e-mail invite to ``board`` as an editor, for ``email``."""
    sent = await create_email_invite(
        db, board.id, owner, email, BoardRole.EDITOR, ConsoleMailer(), settings
    )
    return sent.link.rsplit("/", 1)[1]


async def sign_up(
    client: httpx.AsyncClient, next_path: str | None, email: str = NEWBIE
) -> httpx.Response:
    """POST the sign-up form as the browser does: back to ``/register?next=...``."""
    if CSRF_COOKIE not in client.cookies:
        await client.get("/login")
    return await client.post(
        "/register",
        params={} if next_path is None else {"next": next_path},
        data={
            "email": email,
            "password": DEFAULT_PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )


async def register_page(client: httpx.AsyncClient, next_path: str | None) -> httpx.Response:
    return await client.get("/register", params={} if next_path is None else {"next": next_path})


async def count_users(db) -> int:
    return await db.scalar(select(func.count()).select_from(User))


async def role_of(db, board: Board, email: str) -> BoardRole | None:
    return await db.scalar(
        select(BoardMember.role)
        .join(User, User.id == BoardMember.user_id)
        .where(BoardMember.board_id == board.id, User.email == email)
    )


async def test_link_invite_lets_a_newcomer_sign_up_then_join(closed, db, board, owner):
    token, _ = await link_invite(db, board, owner)
    next_path = f"/invite/{token}"

    page = await register_page(closed, next_path)
    assert page.status_code == 200
    assert 'name="email" type="email" value=""' in page.text  # a link invite takes any address

    r = await sign_up(closed, next_path)
    assert r.status_code == 303
    assert r.headers["location"] == next_path  # the invite page: joining is still a click away
    assert await db.scalar(select(User.id).where(User.email == NEWBIE)) is not None
    assert await role_of(db, board, NEWBIE) is None

    page = await closed.get(next_path)
    assert page.status_code == 200 and "Join board" in page.text
    r = await closed.post(
        f"/invite/{token}/accept", data={"csrf_token": closed.cookies[CSRF_COOKIE]}
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/boards/{board.id}"
    assert await role_of(db, board, NEWBIE) == BoardRole.VIEWER


@pytest.mark.parametrize(
    "next_template",
    [
        None,
        "/boards",
        "//invite/{token}",
        "/invite/{token}?x=1",
        "/invite/{token}/accept",
        "/invite/",
    ],
)
async def test_registration_stays_closed_without_an_invite_page_as_next(
    closed, db, board, owner, next_template
):
    token, _ = await link_invite(db, board, owner)  # valid: only the shape of next refuses
    next_path = None if next_template is None else next_template.format(token=token)

    for r in (await register_page(closed, next_path), await sign_up(closed, next_path)):
        assert r.status_code == 404, (r.request.method, next_path)
        assert "Not found." in r.text  # the same page as with no invite in sight
        assert INVITE_INVALID not in r.text
    assert await count_users(db) == 1  # the owner


@pytest.mark.parametrize("state", ["unknown", "expired", "revoked"])
async def test_a_dead_invite_does_not_open_registration(closed, db, board, owner, state):
    token, invite = await link_invite(db, board, owner)
    if state == "unknown":
        token = "no-such-token"
    elif state == "expired":
        await db.execute(
            update(BoardInvite)
            .where(BoardInvite.token_hash == hash_token(token))
            .values(expires_at=func.now() - timedelta(seconds=1))
        )
        await db.commit()
    else:
        await revoke_invite(db, board.id, invite.id, owner)
    next_path = f"/invite/{token}"

    for r in (await register_page(closed, next_path), await sign_up(closed, next_path)):
        assert r.status_code == 404, r.request.method
        assert INVITE_INVALID in r.text
    assert await count_users(db) == 1


async def test_email_invite_signs_up_only_its_own_address(closed, db, settings, board, owner):
    token = await email_invite(db, settings, board, owner, "friend@example.com")
    next_path = f"/invite/{token}"

    page = await register_page(closed, next_path)
    assert page.status_code == 200
    assert 'value="friend@example.com"' in page.text  # filled in from the invite

    r = await sign_up(closed, next_path, email="mallory@example.com")
    assert r.status_code == 400
    assert INVITE_OTHER_EMAIL in r.text
    assert await count_users(db) == 1

    r = await sign_up(closed, next_path, email=" Friend@Example.COM ")  # Review Focus 5
    assert r.status_code == 303
    assert r.headers["location"] == next_path
    assert await db.scalar(select(User.id).where(User.email == "friend@example.com")) is not None


async def test_used_email_invite_does_not_open_registration(closed, db, settings, board, owner):
    token = await email_invite(db, settings, board, owner, "friend@example.com")
    await db.execute(update(BoardInvite).values(accepted_at=func.now()))
    await db.commit()
    next_path = f"/invite/{token}"

    for r in (
        await register_page(closed, next_path),
        await sign_up(closed, next_path, email="friend@example.com"),
    ):
        assert r.status_code == 404, r.request.method
        assert INVITE_INVALID in r.text
    assert await count_users(db) == 1


async def test_invite_revoked_after_the_sign_up_page_opened_creates_no_account(
    closed, db, board, owner
):
    token, invite = await link_invite(db, board, owner)
    next_path = f"/invite/{token}"
    assert (await register_page(closed, next_path)).status_code == 200

    await revoke_invite(db, board.id, invite.id, owner)
    r = await sign_up(closed, next_path)

    assert r.status_code == 404
    assert INVITE_INVALID in r.text
    assert await count_users(db) == 1


async def test_login_page_from_an_invite_offers_sign_up_while_registration_is_closed(
    closed, db, board, owner
):
    token, _ = await link_invite(db, board, owner)

    page = await closed.get(f"/invite/{token}", follow_redirects=True)  # -> /login?next=...

    assert "Sign in or create an account to join the board." in page.text
    assert f'<a href="/register?next=/invite/{token}" class="link">Create an account</a>' in (
        page.text
    )
    assert "No account yet?" not in page.text  # the foot of the page stays closed
    assert 'href="/register' not in (await closed.get("/login")).text
