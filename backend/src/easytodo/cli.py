"""Administration from the command line, with the app's settings (``EASYTODO_*``)::

    python -m easytodo.cli create-admin --email you@example.com [--password-stdin]
    python -m easytodo.cli import-legacy --legacy-dsn postgresql://user:pass@host:5432/todo_grocery
        --schema prod --owner-email you@example.com [--board-name NAME] [--legacy-tz ZONE]

In the compose setup: ``docker compose exec app python -m easytodo.cli create-admin --email
you@example.com``.
"""

import argparse
import asyncio
import getpass
import sys
from datetime import UTC

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.auth.service import normalize_email
from easytodo.config import get_settings
from easytodo.db import create_engine
from easytodo.errors import ValidationFailed
from easytodo.legacy_import import (
    DEFAULT_BOARD_NAME,
    DEFAULT_LEGACY_TZ,
    UNCATEGORIZED,
    ImportReport,
    import_legacy,
)
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
    import_cmd = commands.add_parser(
        "import-legacy",
        help="copy the data of a v1 database into a new board",
        description="Copy the categories and tasks of a v1 database into a new board of an "
        "existing account. Every run makes another board.",
    )
    import_cmd.add_argument("--legacy-dsn", required=True, help="the v1 database's DSN")
    import_cmd.add_argument("--schema", required=True, help="the v1 schema to read, e.g. prod")
    import_cmd.add_argument(
        "--owner-email", required=True, help="the account that gets the board (it must exist)"
    )
    import_cmd.add_argument(
        "--board-name",
        default=DEFAULT_BOARD_NAME,
        help="the new board's name (default: %(default)s)",
    )
    import_cmd.add_argument(
        "--legacy-tz",
        default=DEFAULT_LEGACY_TZ,
        help="the time zone v1's server ran in; its timestamps have none (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "create-admin":
            output = asyncio.run(_create_admin(args.email, password_stdin=args.password_stdin))
        else:
            output = asyncio.run(_import_legacy(args))
        print(output)
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


async def _import_legacy(args: argparse.Namespace) -> str:
    """Import the v1 data for the account ``args.owner_email``; returns the report to print."""
    email = normalize_email(args.owner_email)
    engine = create_engine(get_settings())
    try:
        async with AsyncSession(engine) as db:
            owner = await db.scalar(select(User).where(User.email == email))
            if owner is None:
                raise ValidationFailed(
                    f"No account with the email {email}. Register it (or run create-admin) first."
                )
            report = await _read_legacy_into(db, owner, args)
    finally:
        await engine.dispose()
    return _describe(report, args.board_name.strip(), email)


async def _read_legacy_into(
    db: AsyncSession, owner: User, args: argparse.Namespace
) -> ImportReport:
    try:
        legacy = await asyncpg.connect(args.legacy_dsn)
    except (asyncpg.PostgresError, OSError) as exc:
        raise ValidationFailed(f"Could not connect to the legacy database: {exc}") from exc
    try:
        return await import_legacy(
            db,
            legacy,
            schema=args.schema,
            owner=owner,
            board_name=args.board_name,
            legacy_tz=args.legacy_tz,
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    except asyncpg.PostgresError as exc:
        raise ValidationFailed(f"Could not read the legacy database: {exc}") from exc
    finally:
        await legacy.close()


def _describe(report: ImportReport, board_name: str, email: str) -> str:
    categories = _count(report.categories, "category", "categories")
    items = _count(report.items, "item", "items")
    lines = [f'Imported {categories} and {items} into the new board "{board_name}" of {email}.']
    if report.orphan_items:
        orphans = _count(report.orphan_items, "item", "items")
        lines.append(f'{orphans} had no category in v1 and went to "{UNCATEGORIZED}".')
    if report.renamed_categories:
        renamed = _count(len(report.renamed_categories), "category", "categories")
        lines.append(
            f"{renamed} got another name in v2 (names are tidied, cut to 120 characters and "
            "unique ignoring case). In v1:"
        )
        lines.extend(f"  {name}" for name in report.renamed_categories)
    return "\n".join(lines)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


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
