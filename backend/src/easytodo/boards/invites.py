"""Board invites. The owner invites by link or by e-mail; only the token's hash is stored.

A link invite can be used by anyone who has the link, any number of times, until it expires
or is revoked. An e-mail invite works once, and only for the account with that address.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.service import normalize_email
from easytodo.boards.permissions import (
    BOARD_GONE,
    Action,
    check_permission,
    is_possible_id,
    require_board,
)
from easytodo.config import Settings
from easytodo.display import user_display_name
from easytodo.errors import Forbidden, NotFound, ValidationFailed
from easytodo.mail.mailer import Mailer, render_mail
from easytodo.models import Board, BoardInvite, BoardMember, BoardRole, NotificationKind, User
from easytodo.notifications.service import notify
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish
from easytodo.security.tokens import hash_token, new_token

INVITE_TTL = timedelta(days=7)
INVITE_INVALID = "This invite is no longer valid."
INVITE_OTHER_EMAIL = "This invite was sent to a different email address."
ROLE_CHOICE = "Choose editor or viewer."


@dataclass(frozen=True)
class InvitePreview:
    """What the invite page shows before the invite is accepted."""

    board_id: int
    board_name: str
    role: BoardRole
    invited_by: str  # the inviting owner's e-mail


@dataclass(frozen=True)
class EmailInvite:
    invite: BoardInvite
    link: str
    delivered: bool  # a mail server took the mail (not so with the console backend)


def invite_url(settings: Settings, raw_token: str) -> str:
    return f"{str(settings.public_url).rstrip('/')}/invite/{raw_token}"


async def _new_invite(
    db: AsyncSession, board_id: int, user: User, role: BoardRole, email: str | None
) -> tuple[str, BoardInvite]:
    if role == BoardRole.OWNER:
        raise ValidationFailed(ROLE_CHOICE)
    raw_token = new_token()
    invite = BoardInvite(
        board_id=board_id,
        token_hash=hash_token(raw_token),
        email=email,
        role=role,
        created_by=user.id,
        expires_at=datetime.now(UTC) + INVITE_TTL,
    )
    db.add(invite)
    await db.commit()
    return raw_token, invite


async def create_link_invite(
    db: AsyncSession, board_id: int, user: User, role: BoardRole
) -> tuple[str, BoardInvite]:
    """A link invite; returns its raw token (shown to the owner once) and the row."""
    await require_board(db, board_id, user, Action.MANAGE_MEMBERS)
    return await _new_invite(db, board_id, user, role, email=None)


async def create_email_invite(
    db: AsyncSession,
    board_id: int,
    user: User,
    email: str,
    role: BoardRole,
    mailer: Mailer,
    settings: Settings,
) -> EmailInvite:
    """An invite for ``email`` (normalised), mailed there with its link. The link comes back too,
    for the owner to pass on when the mail could not be delivered."""
    board, _ = await require_board(db, board_id, user, Action.MANAGE_MEMBERS)
    email = normalize_email(email)
    raw_token, invite = await _new_invite(db, board.id, user, role, email=email)
    subject, body = render_mail(
        "invite",
        inviter=user.email,
        board_name=board.name,
        role=role,
        email=email,
        link=invite_url(settings, raw_token),
        valid_days=INVITE_TTL.days,
    )
    delivered = await mailer.send(email, subject, body)
    return EmailInvite(invite, invite_url(settings, raw_token), delivered)


def _still_usable() -> tuple[ColumnElement[bool], ...]:
    """The conditions under which an invite can still be accepted."""
    return (
        BoardInvite.revoked_at.is_(None),
        BoardInvite.expires_at > func.now(),
        or_(BoardInvite.email.is_(None), BoardInvite.accepted_at.is_(None)),
    )


async def get_valid_invite(db: AsyncSession, raw_token: str) -> BoardInvite:
    """The invite for ``raw_token``; ``NotFound`` if it is unknown, expired or revoked, an e-mail
    invite that was already used, or one to an archived board."""
    invite = await db.scalar(
        select(BoardInvite)
        .join(Board, Board.id == BoardInvite.board_id)
        .where(
            BoardInvite.token_hash == hash_token(raw_token),
            *_still_usable(),
            Board.archived_at.is_(None),
        )
    )
    if invite is None:
        raise NotFound(INVITE_INVALID)
    return invite


async def preview_invite(db: AsyncSession, raw_token: str) -> InvitePreview:
    invite = await get_valid_invite(db, raw_token)
    row = (
        await db.execute(
            select(Board.name, User.email)
            .join(User, User.id == invite.created_by)
            .where(Board.id == invite.board_id)
        )
    ).one_or_none()
    if row is None:  # the board was deleted (with its invites) in the meantime
        raise NotFound(INVITE_INVALID)
    return InvitePreview(invite.board_id, row.name, invite.role, row.email)


async def accept_invite(db: AsyncSession, raw_token: str, user: User) -> Board:
    """Make ``user`` a member with the invite's role and return the board.

    A member already keeps their role (an invite never demotes anyone). An e-mail invite must be
    for ``user``'s address, and is used up even when ``user`` is a member already. The board
    returned is the one read before the member is added, not read again after the commit, when
    another device may have deleted it already (its name and owner go into the notification that
    tells the owner someone joined).
    """
    invite = await get_valid_invite(db, raw_token)
    # FOR KEY SHARE: the board cannot be deleted before the commit (the new member's foreign
    # key holds the same lock), and changes to it do not wait. Taken before the invite's row,
    # in the order a delete of the board locks them (the board, then its invites by cascade),
    # so the two cannot deadlock.
    board = await db.scalar(
        select(Board)
        .where(Board.id == invite.board_id, Board.archived_at.is_(None))
        .with_for_update(read=True, key_share=True)
    )
    if board is None:  # deleted or archived since the invite was read
        raise NotFound(BOARD_GONE)
    if invite.email is not None:
        if normalize_email(user.email) != invite.email:
            raise Forbidden(INVITE_OTHER_EMAIL)
        # Conditional, so two concurrent accepts cannot both use it.
        used = await db.scalar(
            update(BoardInvite)
            .where(BoardInvite.id == invite.id, *_still_usable())
            .values(accepted_at=func.now())
            .returning(BoardInvite.id)
        )
        if used is None:
            raise NotFound(INVITE_INVALID)
    joined = await db.scalar(  # None: a member already, who keeps their role
        insert(BoardMember)
        .values(board_id=board.id, user_id=user.id, role=invite.role)
        .on_conflict_do_nothing(index_elements=[BoardMember.board_id, BoardMember.user_id])
        .returning(BoardMember.user_id)
    )
    events: list[BoardEvent] = []
    if joined:  # the board's members see a new one; the owner is told
        events = [
            BoardEvent(EventType.BOARD_UPDATED, board_id=board.id),
            *notify(
                db,
                [board.owner_id],
                NotificationKind.MEMBER_JOINED,
                board_id=board.id,
                actor=user_display_name(user),
                board=board.name,
            ),
        ]
    await commit_and_publish(db, *events)
    return board


async def list_pending_invites(db: AsyncSession, board_id: int, user: User) -> list[BoardInvite]:
    """The board's invites that can still be accepted, oldest first (for its owner)."""
    board, _ = await require_board(db, board_id, user, Action.MANAGE_MEMBERS)
    invites = await db.scalars(
        select(BoardInvite)
        .where(BoardInvite.board_id == board.id, *_still_usable())
        .order_by(BoardInvite.created_at, BoardInvite.id)
    )
    return list(invites)


async def revoke_invite(db: AsyncSession, board_id: int, invite_id: int, user: User) -> None:
    """Revoke the board's invite (found through ``user``'s memberships, so another board's, also
    one asked for through another board's URL, is ``NotFound``, as is one to an archived board);
    revoking it again changes nothing."""
    row = None
    if is_possible_id(invite_id):
        row = (
            await db.execute(
                select(BoardInvite, BoardMember.role)
                .join(Board, Board.id == BoardInvite.board_id)
                .join(BoardMember, BoardMember.board_id == BoardInvite.board_id)
                .where(
                    BoardInvite.id == invite_id,
                    BoardInvite.board_id == board_id,
                    BoardMember.user_id == user.id,
                    Board.archived_at.is_(None),
                )
            )
        ).one_or_none()
    if row is None:
        raise NotFound("Invite not found.")
    invite, role = row
    check_permission(role, Action.MANAGE_MEMBERS)
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(UTC)
        await db.commit()
