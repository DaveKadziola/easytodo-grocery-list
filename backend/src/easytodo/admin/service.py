"""What the admin panel shows and does: the statistics, the user list, and creating, deleting,
disabling and enabling accounts and turning their 2FA off.

Being an admin opens no board: board pages go through ``require_board`` as for anyone. There is
always an active admin left: the last one can be neither deleted nor disabled.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnExpressionArgument, ScalarSelect, Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.attachments.service import total_bytes
from easytodo.auth.reset import invalidate_reset_tokens
from easytodo.auth.service import DEFAULT_BOARD_NAME, normalize_email
from easytodo.auth.sessions import revoke_all_sessions, sessions_revoked
from easytodo.boards.permissions import is_possible_id
from easytodo.boards.service import add_board
from easytodo.errors import Conflict, NotFound
from easytodo.models import (
    Board,
    BoardMember,
    Category,
    Item,
    RecoveryCode,
    User,
    UserSession,
    UserStatus,
)
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish
from easytodo.security.passwords import hash_password_async

ACTIVE_WINDOW = timedelta(days=30)  # "active" users signed in within it
CANNOT_DELETE_SELF = "You can't delete your own account here."
LAST_ADMIN = "At least one active admin is required."
USER_NOT_FOUND = "User not found."
# Also what a refresh of the "Account created" page gets (the form is posted again).
EMAIL_TAKEN = "This account already exists. Use Send password reset to give them a new password."
_LIKE_ESCAPE = "/"


@dataclass(frozen=True)
class AdminStats:
    users_total: int
    users_active_30d: int  # signed in within the last 30 days (``last_login_at``)
    users_with_2fa: int
    boards_total: int
    shared_boards: int  # with more than one member
    categories_total: int
    items_total: int
    items_open: int
    attachments_bytes: int  # the size of every attached file


def _page_count(total: int, per_page: int) -> int:
    """How many pages ``total`` rows take (at least 1, an empty one)."""
    return max(1, -(-total // per_page))


@dataclass
class Page[T]:
    items: list[T]
    total: int  # matching rows on every page
    page: int  # 1-based
    per_page: int

    @property
    def pages(self) -> int:
        return _page_count(self.total, self.per_page)


@dataclass(frozen=True)
class UserRow:
    id: int
    email: str
    is_admin: bool
    status: UserStatus
    totp_enabled: bool
    created_at: datetime
    last_login_at: datetime | None
    owned_boards: int


async def get_stats(db: AsyncSession) -> AdminStats:
    """Every count in one query."""

    def count(entity: type, *where: ColumnExpressionArgument[bool]) -> ScalarSelect[int]:
        return select(func.count()).select_from(entity).where(*where).scalar_subquery()

    shared = (
        select(BoardMember.board_id)
        .group_by(BoardMember.board_id)
        .having(func.count() > 1)
        .subquery()
    )
    row = (
        await db.execute(
            select(
                count(User),
                count(User, User.last_login_at >= func.now() - ACTIVE_WINDOW),
                count(User, User.totp_enabled),
                count(Board),
                select(func.count()).select_from(shared).scalar_subquery(),
                count(Category),
                count(Item, Item.deleted_at.is_(None)),
                count(Item, Item.deleted_at.is_(None), Item.is_done.is_(False)),
                total_bytes,
            )
        )
    ).one()
    return AdminStats(*row)


def _user_rows() -> Select:
    owned_boards = (
        select(func.count()).select_from(Board).where(Board.owner_id == User.id).scalar_subquery()
    )
    return select(
        User.id,
        User.email,
        User.is_admin,
        User.status,
        User.totp_enabled,
        User.created_at,
        User.last_login_at,
        owned_boards,
    )


def _escape_like(value: str) -> str:
    """``value`` matched literally in a LIKE pattern (``ESCAPE '/'``): ``%`` and ``_`` are no
    wildcards."""
    for char in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(char, _LIKE_ESCAPE + char)
    return value


async def list_users(
    db: AsyncSession, *, q: str | None, page: int, per_page: int = 50
) -> Page[UserRow]:
    """The users whose e-mail contains ``q`` (ignoring case; all of them without ``q``), by
    e-mail. A page past the last shows the last one, a page before the first the first."""
    where: list[ColumnExpressionArgument[bool]] = []
    q = q.replace("\x00", "") if q else q  # PostgreSQL text cannot hold NUL
    if q:
        where.append(User.email.ilike(f"%{_escape_like(q)}%", escape=_LIKE_ESCAPE))
    total = await db.scalar(select(func.count()).select_from(User).where(*where))
    page = min(max(page, 1), _page_count(total, per_page))
    rows = await db.execute(
        _user_rows()
        .where(*where)
        .order_by(User.email)
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    return Page([UserRow(*row) for row in rows], total, page, per_page)


async def get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id) if is_possible_id(user_id) else None
    if user is None:
        raise NotFound(USER_NOT_FOUND)
    return user


async def get_user_row(db: AsyncSession, user_id: int) -> UserRow:
    row = (await db.execute(_user_rows().where(User.id == user_id))).one_or_none()
    if row is None:
        raise NotFound(USER_NOT_FOUND)
    return UserRow(*row)


async def create_user_with_temp_password(
    db: AsyncSession, email: str, *, is_admin: bool = False
) -> tuple[User, str]:
    """A new account with a random temporary password, which it must change when it first signs
    in, and its first board, "My list" (as at registration); returns the account and the
    password (to be shown this once)."""
    email = normalize_email(email)
    password = secrets.token_urlsafe(12)
    user = User(
        email=email,
        password_hash=await hash_password_async(password),
        is_admin=is_admin,
        must_change_password=True,
    )
    try:
        # A savepoint: a failed insert must not expire the signed-in admin the page shows.
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:  # users.email is unique
        raise Conflict(EMAIL_TAKEN) from None
    await add_board(db, user, DEFAULT_BOARD_NAME)
    await db.commit()
    return user, password


async def _keep_an_active_admin(db: AsyncSession, user: User) -> None:
    """``Conflict`` if ``user`` is the only active admin, so they may not go.

    Locks the active admins' rows until the commit, FOR NO KEY UPDATE: two admins removing each
    other at once cannot both succeed (that lock conflicts with itself), and a row that points at
    an admin (a notification for them) can still be inserted, whose foreign key takes only FOR KEY
    SHARE. A plain FOR UPDATE would make a member leaving a board, or someone joining one, wait
    behind the removal.
    """
    if not user.is_admin or user.status != UserStatus.ACTIVE:
        return
    admins = await db.scalars(
        select(User.id)
        .where(User.is_admin, User.status == UserStatus.ACTIVE)
        .order_by(User.id)
        .with_for_update(key_share=True)
    )
    if not any(admin_id != user.id for admin_id in admins):
        raise Conflict(LAST_ADMIN)


async def delete_user(db: AsyncSession, admin: User, user_id: int) -> None:
    """Delete the account; the boards it owns go with it (FK cascades), and its devices are
    signed out."""
    if user_id == admin.id:
        raise Conflict(CANNOT_DELETE_SELF)
    user = await get_user(db, user_id)
    await _keep_an_active_admin(db, user)
    # The account's row first, as in ``revoke_all_sessions`` (not in ``get_user``: that would lock
    # before ``_keep_an_active_admin``'s locks, which are in id order, and let two admins deleting
    # each other deadlock instead of getting ``LAST_ADMIN``).
    await db.execute(select(User.id).where(User.id == user.id).with_for_update(key_share=True))
    owned = list(await db.scalars(select(Board.id).where(Board.owner_id == user.id)))
    # Deleted here rather than by the cascade, so their sockets are closed too.
    revoked = await revoke_all_sessions(db, user.id)
    await db.delete(user)
    await commit_and_publish(
        db,
        *sessions_revoked(user.id, revoked),
        *(BoardEvent(EventType.BOARD_DELETED, board_id=board_id) for board_id in owned),
    )


async def set_status(db: AsyncSession, admin: User, user_id: int, status: UserStatus) -> None:
    """Disable the account (it is signed out everywhere, its reset links end, and it cannot sign
    in) or enable it. Enabling it again does not bring the links back: a link mailed before is
    gone for good, and a new one has to be asked for."""
    user = await get_user(db, user_id)
    revoked: list[int] = []
    disabling = status == UserStatus.DISABLED
    if disabling:
        await _keep_an_active_admin(db, user)
    # The account's row is locked first, as in ``revoke_all_sessions``: by the UPDATE of the status
    # (flushed now, no statement added), or, if the status stays, by a SELECT (a flush of nothing
    # locks nothing, and a disabled account can still have sessions and links to end).
    if user.status != status:
        user.status = status
        await db.flush()
    elif disabling:
        await db.execute(select(User.id).where(User.id == user.id).with_for_update(key_share=True))
    if disabling:
        revoked = await revoke_all_sessions(db, user.id)
        await invalidate_reset_tokens(db, user.id)
    await commit_and_publish(db, *sessions_revoked(user.id, revoked))


async def reset_2fa(db: AsyncSession, admin: User, user_id: int) -> None:
    """Turn the account's 2FA off, as ``twofa.disable_2fa`` does but without its password and
    code (for a lost authenticator): the secret and the recovery codes are deleted, and sign-ins
    still waiting for a code end."""
    user = await get_user(db, user_id)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    pending = await db.scalars(
        delete(UserSession)
        .where(UserSession.user_id == user.id, UserSession.mfa_pending.is_(True))
        .returning(UserSession.id)
    )
    await commit_and_publish(db, *sessions_revoked(user.id, list(pending)))
