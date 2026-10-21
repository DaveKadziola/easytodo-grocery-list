"""Administration from the command line, with the app's settings (``EASYTODO_*``)::

    python -m easytodo.cli create-admin --email you@example.com [--password-stdin]

In the compose setup: ``docker compose exec app python -m easytodo.cli create-admin --email
you@example.com``.
"""

import argparse
import asyncio
import getpass
import sys
from datetime import UTC

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.service import normalize_email
from easytodo.config import get_settings
from easytodo.db import create_engine
from easytodo.errors import ValidationFailed
from easytodo.models import User, UserSession
from easytodo.security.passwords import hash_password, validate_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m easytodo.cli", description="EasyToDo admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create_admin = commands.add_parser(
        "create-admin",
        help="create an admin account, or make an existing account an admin",
        description="Create an admin account, or make an existing account an admin (its "
        "password stays as it is).",
    )
    create_admin.add_argument("--email", required=True, help="the account's email address")
    create_admin.add_argument(
        "--password-stdin",
        action="store_true",
        help="read a new account's password from the first line of standard input instead of "
        "asking for it",
    )
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(_create_admin(args.email, password_stdin=args.password_stdin)))
    except ValidationFailed as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0


async def _create_admin(email: str, *, password_stdin: bool) -> str:
    """Make the account with ``email`` an admin, creating it if there is none (with the password
    from standard input or typed in); returns what was done."""
    email = normalize_email(email)
    engine = create_engine(get_settings())
    try:
        async with AsyncSession(engine) as db:
            user = await db.scalar(select(User).where(User.email == email))
            if user is not None and user.is_admin:
                return f"{email} is already an admin."
            if user is not None:
                warning = await _promotion_warning(db, user, password_stdin=password_stdin)
                user.is_admin = True
                await db.commit()
                print(warning, file=sys.stderr)
                return f"{email} is now an admin. Their password is unchanged."
            password = _read_password(from_stdin=password_stdin)
            validate_password(password)
            db.add(User(email=email, password_hash=hash_password(password), is_admin=True))
            await db.commit()
            return f"Created the admin account {email}."
    finally:
        await engine.dispose()


async def _promotion_warning(db: AsyncSession, user: User, *, password_stdin: bool) -> str:
    """What to tell the operator when an existing account is promoted: registration is open by
    default, so someone else may have registered the address first."""
    sessions = await db.scalar(
        select(func.count())
        .select_from(UserSession)
        .where(UserSession.user_id == user.id, UserSession.expires_at > func.now())
    )
    warning = (
        f"warning: {user.email} already existed (registered "
        f"{user.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, {sessions} active session(s)); "
        "it was promoted with its existing password."
    )
    if password_stdin:
        warning += " The password from standard input was not used."
    return warning


def _read_password(*, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat the password: ") != password:
        raise ValidationFailed("Passwords do not match.")
    return password


if __name__ == "__main__":
    sys.exit(main())
