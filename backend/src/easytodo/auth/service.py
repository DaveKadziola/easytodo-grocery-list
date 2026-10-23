"""Accounts: e-mail normalisation, registration and password sign-in with lockout."""

from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.service import create_board
from easytodo.errors import AppError, Conflict, ValidationFailed
from easytodo.models import User, UserStatus
from easytodo.security.passwords import (
    MAX_PASSWORD_LENGTH,
    hash_password,
    hash_password_async,
    needs_rehash,
    validate_password,
    verify_password_async,
)

DEFAULT_BOARD_NAME = "My list"
MAX_FAILED_LOGINS = 10
LOCKOUT_DURATION = timedelta(minutes=15)

# Checked against for unknown e-mails, so they cost the same argon2 work as a wrong password.
DUMMY_HASH = hash_password("easytodo-dummy-password")


class InvalidCredentials(AppError):
    def __init__(self) -> None:
        super().__init__("Invalid email or password.")


class AccountLocked(AppError):
    def __init__(self) -> None:
        super().__init__("Too many failed attempts. Try again in 15 minutes.")


class AccountDisabled(AppError):
    def __init__(self) -> None:
        super().__init__("This account is disabled.")


def normalize_email(email: str) -> str:
    """The address stripped and lower-cased; ``ValidationFailed`` unless it is valid."""
    email = email.strip().lower()
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError:
        raise ValidationFailed("Enter a valid email address.") from None
    return email


async def register_user(db: AsyncSession, email: str, password: str) -> User:
    """A new account with its first board, "My list" (one transaction)."""
    email = normalize_email(email)
    validate_password(password)
    user = User(email=email, password_hash=await hash_password_async(password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:  # users.email is unique; also covers two concurrent submits
        await db.rollback()
        raise Conflict("An account with this email already exists.") from None
    await create_board(db, user, DEFAULT_BOARD_NAME)  # commits the user too
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    """The active user with these credentials. 10 failures in a row lock it for 15 minutes.

    The password is checked on a thread, so other requests run meanwhile: a lock set in that time
    (the tenth wrong password of a parallel attempt) must still win over this right password.
    That is what the guarded update at the end is for."""
    if len(password) > MAX_PASSWORD_LENGTH:  # refuse before hashing an arbitrarily long input
        raise InvalidCredentials()
    user = await _find_user(db, email)
    if user is None:
        await verify_password_async(DUMMY_HASH, password)
        raise InvalidCredentials()
    now = datetime.now(UTC)
    if user.locked_until is not None and user.locked_until > now:
        raise AccountLocked()
    if not await verify_password_async(user.password_hash, password):
        raise await _failed_attempt(db, user, now)
    if user.status == UserStatus.DISABLED:
        raise AccountDisabled()
    changes: dict[str, object] = {
        "failed_login_count": 0,
        "locked_until": None,
        "last_login_at": now,
    }
    if needs_rehash(user.password_hash):  # before the update, which then holds its lock briefly
        changes["password_hash"] = await hash_password_async(password)
    signed_in = await db.scalar(
        update(User)
        .where(User.id == user.id, or_(User.locked_until.is_(None), User.locked_until <= now))
        .values(changes)
        .returning(User.id)
    )
    if signed_in is None:  # locked meanwhile
        # Obligatory: with no row matched the update has still written its values to ``user``
        # (it judged the where clause on the stale object), and the rollback drops them.
        await db.rollback()
        raise AccountLocked()
    await db.commit()
    return user


async def _find_user(db: AsyncSession, email: str) -> User | None:
    try:
        email = normalize_email(email)
    except ValidationFailed:
        return None  # no account has an invalid address
    return await db.scalar(select(User).where(User.email == email))


async def _failed_attempt(db: AsyncSession, user: User, now: datetime) -> AppError:
    """Count a wrong password (atomically, so parallel attempts all count); returns the error."""
    failures = await db.scalar(
        update(User)
        .where(User.id == user.id)
        .values(failed_login_count=User.failed_login_count + 1)
        .returning(User.failed_login_count)
    )
    locked = failures >= MAX_FAILED_LOGINS
    if locked:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(failed_login_count=0, locked_until=now + LOCKOUT_DURATION)
        )
    await db.commit()
    return AccountLocked() if locked else InvalidCredentials()
