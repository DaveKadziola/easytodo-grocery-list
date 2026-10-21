"""Importing the data of the v1 app (Flask, one shared list) into a new v2 board.

v1 keeps ``categories``, ``tasks`` and ``task_assignment`` in a schema of its own (``prod``, say).
Categories become the board's categories in v1's order, tasks become items in the category they
are assigned to (the latest assignment counts) or in "Uncategorized".
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.service import add_board
from easytodo.content.validation import (
    CATEGORY_NAME_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
)
from easytodo.errors import ValidationFailed
from easytodo.models import Category, Item, User

DEFAULT_BOARD_NAME = "Imported from v1"
DEFAULT_LEGACY_TZ = "Europe/Warsaw"  # the time zone v1's server ran in
UNCATEGORIZED = "Uncategorized"  # where tasks without a category go
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")  # fullmatch: "$" would let a trailing newline in

_CATEGORIES = (
    'SELECT id, name, created_at, updated_at FROM "{schema}".categories ORDER BY position, id'
)
# One row per task, with the category of its newest assignment (none: NULL).
_TASKS = """
SELECT DISTINCT ON (t.id) t.id, t.name, t.description, t.is_done, t.created_at, t.updated_at,
       a.category_id
FROM "{schema}".tasks t
LEFT JOIN "{schema}".task_assignment a ON a.task_id = t.id
ORDER BY t.id, a.assigned_at DESC NULLS LAST, a.id DESC
"""


@dataclass
class ImportReport:
    categories: int  # created on the board, "Uncategorized" included
    items: int
    renamed_categories: list[str]  # v1 names that are different in v2 (cut, tidied, made unique)
    orphan_items: int  # tasks without a category in v1


async def import_legacy(
    db: AsyncSession,
    legacy: asyncpg.Connection,
    *,
    schema: str,
    owner: User,
    board_name: str = DEFAULT_BOARD_NAME,
    legacy_tz: str = DEFAULT_LEGACY_TZ,
) -> ImportReport:
    """Copy the v1 data in ``schema`` into a new board of ``owner``.

    v1 timestamps have no zone: they are read as ``legacy_tz`` local time. All of it happens in
    ``db``'s transaction, which is committed at the end, or rolled back if anything fails.
    Raises ``ValueError`` for a bad ``schema`` or ``legacy_tz`` (before anything is read).
    """
    if not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(f"Invalid schema name {schema!r}: use lowercase letters, digits and _.")
    try:
        zone = ZoneInfo(legacy_tz)
    except (KeyError, ValueError) as exc:  # not found, or not a name at all
        raise ValueError(f"Unknown time zone {legacy_tz!r}.") from exc

    async with legacy.transaction(isolation="repeatable_read", readonly=True):  # one snapshot
        categories = await legacy.fetch(_CATEGORIES.format(schema=schema))
        tasks = await legacy.fetch(_TASKS.format(schema=schema))
    try:
        report = await _write(db, owner, board_name, categories, tasks, zone)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return report


async def _write(
    db: AsyncSession,
    owner: User,
    board_name: str,
    legacy_categories: list[asyncpg.Record],
    legacy_tasks: list[asyncpg.Record],
    zone: tzinfo,
) -> ImportReport:
    now = datetime.now(UTC)

    def when(value: datetime | None) -> datetime | None:
        return None if value is None else value.replace(tzinfo=zone)

    board = await add_board(db, owner, board_name)
    by_name: dict[str, Category] = {}  # by lower-cased name: v2 names are unique that way
    by_legacy_id: dict[int, Category] = {}
    renamed: list[str] = []
    for position, row in enumerate(legacy_categories):
        name = _unique(
            _fit_name(row["name"], CATEGORY_NAME_MAX_LENGTH, f"category {row['id']}"), by_name
        )
        if name != row["name"]:
            renamed.append(row["name"])
        created = when(row["created_at"]) or now
        category = Category(
            board_id=board.id,
            name=name,
            position=position,
            created_at=created,
            updated_at=when(row["updated_at"]) or created,
        )
        by_name[name.lower()] = by_legacy_id[row["id"]] = category
    orphans = sum(1 for task in legacy_tasks if task["category_id"] is None)
    if orphans and UNCATEGORIZED.lower() not in by_name:
        by_name[UNCATEGORIZED.lower()] = Category(
            board_id=board.id, name=UNCATEGORIZED, position=len(legacy_categories)
        )
    created_categories = list(by_name.values())
    db.add_all(created_categories)
    await db.flush()  # the categories' ids

    items = []
    for task in legacy_tasks:
        category = (
            by_name[UNCATEGORIZED.lower()]
            if task["category_id"] is None
            else by_legacy_id[task["category_id"]]
        )
        created = when(task["created_at"]) or now
        updated = when(task["updated_at"]) or created
        is_done = bool(task["is_done"])
        items.append(
            Item(
                board_id=board.id,
                category_id=category.id,
                name=_fit_name(task["name"], ITEM_NAME_MAX_LENGTH, f"task {task['id']}"),
                description=_fit_description(task["description"]),
                is_done=is_done,
                done_at=updated if is_done else None,  # v1 does not know when: the last change
                created_at=created,
                updated_at=updated,
            )
        )
    db.add_all(items)
    return ImportReport(
        categories=len(created_categories),
        items=len(items),
        renamed_categories=renamed,
        orphan_items=orphans,
    )


def _fit_name(value: str, max_len: int, what: str) -> str:
    """``value`` as a name typed into v2 is stored (whitespace collapsed), cut to ``max_len``
    characters."""
    name = " ".join(value.split())
    if not name:
        raise ValidationFailed(f"v1 {what} has a blank name.")
    return name[:max_len].rstrip()


def _fit_description(value: str | None) -> str | None:
    """``value`` as a description typed into v2 is stored, cut to the limit; None if blank."""
    description = (value or "").replace("\r\n", "\n").strip()
    return description[:DESCRIPTION_MAX_LENGTH].rstrip() or None


def _unique(name: str, taken: dict[str, Category]) -> str:
    """``name``, or ``name`` with " (2)", " (3)"... when ``taken`` (keyed by lower-cased name)
    has it already; the suffix fits in the category name limit."""
    candidate, number = name, 1
    while candidate.lower() in taken:
        number += 1
        suffix = f" ({number})"
        candidate = name[: CATEGORY_NAME_MAX_LENGTH - len(suffix)].rstrip() + suffix
    return candidate
