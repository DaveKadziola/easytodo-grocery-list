"""Importing the v1 data (``categories``, ``tasks``, ``task_assignment``) into a v2 board, from the
function and from ``python -m easytodo.cli import-legacy``."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest
from factories import create_user
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo import legacy_import
from easytodo.boards.service import get_board_view
from easytodo.cli import _describe, main
from easytodo.content.validation import clean_description, clean_name
from easytodo.errors import ValidationFailed
from easytodo.legacy_import import ImportReport, import_legacy
from easytodo.models import Board, BoardMember, BoardRole, Category, Item

SCHEMA = "legacy_t"

# The v1 tables as in ddl/3_3-ddl_prod.sql of the git tag legacy-v1 (columns, defaults,
# constraints), without the triggers, in a schema of the test database.
LEGACY_DDL = f"""
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.categories (
    id serial4 NOT NULL,
    "name" varchar(255) NOT NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    "position" int4 DEFAULT 0 NULL,
    CONSTRAINT categories_name_key UNIQUE (name),
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);
CREATE TABLE {SCHEMA}.tasks (
    id serial4 NOT NULL,
    "name" varchar(255) NOT NULL,
    description text NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    is_done bool DEFAULT false NULL,
    CONSTRAINT tasks_pkey PRIMARY KEY (id)
);
CREATE TABLE {SCHEMA}.task_assignment (
    id serial4 NOT NULL,
    task_id int4 NOT NULL,
    category_id int4 NOT NULL,
    assigned_at timestamp DEFAULT now() NULL,
    CONSTRAINT task_assignment_pkey PRIMARY KEY (id),
    CONSTRAINT fk_category FOREIGN KEY (category_id) REFERENCES {SCHEMA}.categories(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_task FOREIGN KEY (task_id) REFERENCES {SCHEMA}.tasks(id) ON DELETE CASCADE
);
"""

SUMMER = datetime(2026, 7, 1, 12, 0)  # v1 stores local time without a zone
WINTER = datetime(2026, 1, 15, 12, 0)


@pytest.fixture
def legacy_dsn(settings) -> str:
    """The test database's DSN as asyncpg takes it (the v1 schema lives in that database)."""
    return settings.database_url.get_secret_value().replace("+asyncpg", "")


@pytest.fixture
async def legacy_conn(legacy_dsn) -> AsyncIterator[asyncpg.Connection]:
    """A connection to the test database with an empty v1 schema in it."""
    conn = await asyncpg.connect(legacy_dsn)
    await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await conn.execute(LEGACY_DDL)
    yield conn
    await conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
    await conn.close()


@pytest.fixture
async def owner(db):
    return await create_user(db, "dave@example.com")


async def add_category(
    conn, name: str, position: int | None = 0, *, created_at=SUMMER, updated_at=SUMMER
) -> int:
    return await conn.fetchval(
        f"INSERT INTO {SCHEMA}.categories (name, position, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        name,
        position,
        created_at,
        updated_at,
    )


async def assign(conn, task_id: int, category_id: int, assigned_at=SUMMER) -> None:
    await conn.execute(
        f"INSERT INTO {SCHEMA}.task_assignment (task_id, category_id, assigned_at) "
        "VALUES ($1, $2, $3)",
        task_id,
        category_id,
        assigned_at,
    )


async def add_task(
    conn,
    name: str,
    category_id: int | None = None,
    *,
    description: str | None = None,
    is_done: bool = False,
    created_at=SUMMER,
    updated_at=SUMMER,
) -> int:
    task_id = await conn.fetchval(
        f"INSERT INTO {SCHEMA}.tasks (name, description, is_done, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        name,
        description,
        is_done,
        created_at,
        updated_at,
    )
    if category_id is not None:
        await assign(conn, task_id, category_id)
    return task_id


async def run_import(db, legacy_conn, owner, **kwargs) -> ImportReport:
    report = await import_legacy(db, legacy_conn, schema=SCHEMA, owner=owner, **kwargs)
    db.expunge_all()  # what the tests read below comes from the database, not from the session
    return report


async def only_board(db) -> Board:
    return await db.scalar(select(Board))


async def categories_of(db, board: Board) -> list[Category]:
    return list(
        await db.scalars(
            select(Category).where(Category.board_id == board.id).order_by(Category.position)
        )
    )


async def board_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(Board))


async def test_imports_categories_in_order_with_items(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 2)
    bakery = await add_category(legacy_conn, "Bakery", 1)
    await add_task(
        legacy_conn,
        "Milk",
        dairy,
        description="2 liters",
        is_done=True,
        created_at=datetime(2026, 7, 1, 9, 0),
        updated_at=datetime(2026, 7, 2, 18, 30),
    )
    await add_task(legacy_conn, "Butter", dairy)
    await add_task(legacy_conn, "Bread", bakery, description="  \n ")  # blank: no description

    report = await run_import(db, legacy_conn, owner)

    assert report == ImportReport(categories=2, items=3, renamed_categories=[], orphan_items=0)
    board = await only_board(db)
    assert board.name == "Imported from v1"
    assert board.owner_id == owner.id
    member = await db.get(BoardMember, (board.id, owner.id))
    assert member.role == BoardRole.OWNER
    view = await get_board_view(db, board.id, owner)
    assert [c.category.name for c in view.categories] == ["Bakery", "Dairy"]  # by position
    assert [c.category.position for c in view.categories] == [0, 1]
    bakery_view, dairy_view = view.categories
    assert [i.item.name for i in bakery_view.items] == ["Bread"]
    assert bakery_view.items[0].item.description is None
    butter, milk = (iv.item for iv in dairy_view.items)  # open items first
    assert (butter.name, butter.is_done, butter.done_at) == ("Butter", False, None)
    assert (milk.name, milk.description, milk.is_done) == ("Milk", "2 liters", True)
    assert milk.board_id == board.id
    assert milk.created_by == owner.id  # v1 knows no authors: the importing account stands in
    # v1 has no completion time: the last change stands in for it.
    assert milk.created_at == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)  # Warsaw is UTC+2 in July
    assert milk.updated_at == milk.done_at == datetime(2026, 7, 2, 16, 30, tzinfo=UTC)


async def test_orphan_tasks_go_to_uncategorized(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(legacy_conn, "Milk", dairy)
    await add_task(legacy_conn, "Batteries")
    await add_task(legacy_conn, "Light bulbs", is_done=True)

    report = await run_import(db, legacy_conn, owner)

    assert report == ImportReport(categories=2, items=3, renamed_categories=[], orphan_items=2)
    categories = await categories_of(db, await only_board(db))
    assert [c.name for c in categories] == ["Dairy", "Uncategorized"]  # the new one goes last
    items = await db.scalars(select(Item).where(Item.category_id == categories[1].id))
    assert sorted(i.name for i in items) == ["Batteries", "Light bulbs"]


async def test_orphans_join_an_existing_uncategorized_category(db, legacy_conn, owner):
    existing = await add_category(legacy_conn, "UNCATEGORIZED", 1)
    await add_task(legacy_conn, "Milk", existing)
    await add_task(legacy_conn, "Batteries")

    report = await run_import(db, legacy_conn, owner)

    assert report == ImportReport(categories=1, items=2, renamed_categories=[], orphan_items=1)
    (category,) = await categories_of(db, await only_board(db))
    assert category.name == "UNCATEGORIZED"
    items = await db.scalars(select(Item).where(Item.category_id == category.id))
    assert sorted(i.name for i in items) == ["Batteries", "Milk"]


async def test_an_empty_v1_schema_makes_an_empty_board(db, legacy_conn, owner):
    report = await run_import(db, legacy_conn, owner)

    assert report == ImportReport(categories=0, items=0, renamed_categories=[], orphan_items=0)
    assert await categories_of(db, await only_board(db)) == []


async def test_the_latest_assignment_wins(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    bakery = await add_category(legacy_conn, "Bakery", 2)
    moved = await add_task(legacy_conn, "Milk")
    await assign(legacy_conn, moved, dairy, datetime(2026, 7, 1, 10, 0))
    await assign(legacy_conn, moved, bakery, datetime(2026, 7, 3, 10, 0))
    same_time = await add_task(legacy_conn, "Bread")  # a tie: the assignment made last wins
    await assign(legacy_conn, same_time, dairy, datetime(2026, 7, 1, 10, 0))
    await assign(legacy_conn, same_time, bakery, datetime(2026, 7, 1, 10, 0))
    unstamped = await add_task(legacy_conn, "Eggs")  # an assignment without a time is the oldest
    await assign(legacy_conn, unstamped, bakery, datetime(2026, 7, 1, 10, 0))
    await legacy_conn.execute(
        f"INSERT INTO {SCHEMA}.task_assignment (task_id, category_id, assigned_at) "
        "VALUES ($1, $2, NULL)",
        unstamped,
        dairy,
    )

    report = await run_import(db, legacy_conn, owner)

    assert (report.items, report.orphan_items) == (3, 0)  # one item per task, not per assignment
    view = await get_board_view(db, (await only_board(db)).id, owner)
    placed = {c.category.name: [i.item.name for i in c.items] for c in view.categories}
    assert placed == {"Dairy": [], "Bakery": ["Bread", "Eggs", "Milk"]}


async def test_import_is_atomic(db, legacy_conn, owner, monkeypatch):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(legacy_conn, "Milk", dairy)
    await add_task(legacy_conn, "Butter", dairy)

    def boom(value):
        raise RuntimeError("boom")

    # The board and its categories are already written (not committed) when the items are built.
    monkeypatch.setattr(legacy_import, "_fit_description", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await import_legacy(db, legacy_conn, schema=SCHEMA, owner=owner)

    async with AsyncSession(db.bind) as other:  # a new connection sees only what was committed
        assert await board_count(other) == 0
        assert await other.scalar(select(func.count()).select_from(Category)) == 0
        assert await other.scalar(select(func.count()).select_from(Item)) == 0
        assert await other.scalar(select(func.count()).select_from(BoardMember)) == 0


@pytest.mark.parametrize(
    "schema", ["prod; drop table x", "Prod", "", "1prod", 'pr"od', "prod\n", "pro-d", "a b"]
)
async def test_invalid_schema_name_rejected(db, legacy_conn, owner, schema):
    with pytest.raises(ValueError, match="schema"):
        await import_legacy(db, legacy_conn, schema=schema, owner=owner)

    assert await board_count(db) == 0


async def test_naive_timestamps_converted_from_legacy_tz(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1, created_at=WINTER, updated_at=SUMMER)
    await add_task(legacy_conn, "Milk", dairy, created_at=WINTER, updated_at=SUMMER)

    # v1 ran in Europe/Warsaw: UTC+1 in winter, +2 in summer.
    await run_import(db, legacy_conn, owner)

    (category,) = await categories_of(db, await only_board(db))
    item = await db.scalar(select(Item))
    assert category.created_at == item.created_at == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    assert category.updated_at == item.updated_at == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


async def test_clock_change_times_take_the_offset_before_the_change(db, legacy_conn, owner):
    # Warsaw: 02:30 on 2026-03-29 does not exist (02:00 becomes 03:00), 02:30 on 2026-10-25
    # happens twice (03:00 becomes 02:00). Both are read with the offset in force before the change.
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(
        legacy_conn,
        "Milk",
        dairy,
        created_at=datetime(2026, 3, 29, 2, 30),
        updated_at=datetime(2026, 10, 25, 2, 30),
    )

    await run_import(db, legacy_conn, owner)

    item = await db.scalar(select(Item))
    assert item.created_at == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # UTC+1
    assert item.updated_at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # UTC+2


async def test_legacy_tz_can_be_chosen(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(legacy_conn, "Milk", dairy, created_at=SUMMER)

    await run_import(db, legacy_conn, owner, legacy_tz="America/New_York")

    assert (await db.scalar(select(Item))).created_at == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)


async def test_unknown_legacy_tz_rejected(db, legacy_conn, owner):
    with pytest.raises(ValueError, match="time zone"):
        await import_legacy(db, legacy_conn, schema=SCHEMA, owner=owner, legacy_tz="Mars/Olympus")

    assert await board_count(db) == 0


async def test_missing_timestamps_fall_back_to_the_import_time(db, legacy_conn, owner):
    category_id = await legacy_conn.fetchval(
        f"INSERT INTO {SCHEMA}.categories (name, position, created_at, updated_at) "
        "VALUES ('Dairy', NULL, NULL, NULL) RETURNING id"
    )
    task_id = await legacy_conn.fetchval(
        f"INSERT INTO {SCHEMA}.tasks (name, created_at, updated_at, is_done) "
        "VALUES ('Milk', NULL, $1, NULL) RETURNING id",
        WINTER,
    )
    await assign(legacy_conn, task_id, category_id)
    before = datetime.now(UTC)

    await run_import(db, legacy_conn, owner)

    (category,) = await categories_of(db, await only_board(db))
    item = await db.scalar(select(Item))
    assert before <= category.created_at <= category.updated_at <= datetime.now(UTC)
    assert before <= item.created_at <= datetime.now(UTC)
    assert item.updated_at == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    assert item.is_done is False  # NULL in v1


async def test_names_are_cut_to_the_v2_limits(db, legacy_conn, owner):
    long_category = "C" * 255
    dairy = await add_category(legacy_conn, long_category, 1)
    await add_category(legacy_conn, "Bakery", 2)
    await add_task(legacy_conn, "I" * 255, dairy, description="D" * 3000)

    report = await run_import(db, legacy_conn, owner)

    assert report.renamed_categories == [long_category]  # the v1 name, as the operator knows it
    category, _ = await categories_of(db, await only_board(db))
    assert category.name == "C" * 120
    item = await db.scalar(select(Item))
    assert item.name == "I" * 200
    assert item.description == "D" * 2000


async def test_names_are_tidied_like_typed_ones(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy   products", 1)
    await add_task(legacy_conn, "  Whole \t milk ", dairy, description=" a\r\nb ")

    report = await run_import(db, legacy_conn, owner)

    assert report.renamed_categories == ["Dairy   products"]
    (category,) = await categories_of(db, await only_board(db))
    assert category.name == "Dairy products"
    item = await db.scalar(select(Item))
    assert (item.name, item.description) == ("Whole milk", "a\nb")


@pytest.mark.parametrize("value", ["  Whole \t milk ", "Mi\x00lk \r\n"])
def test_v1_text_follows_the_rules_of_typed_text(value):
    assert legacy_import._fit_name(value, 200, "task 1") == clean_name(
        value, max_len=200, label="Item name"
    )
    assert legacy_import._fit_description(value) == clean_description(value)


async def test_categories_without_a_position_go_last(db, legacy_conn, owner):
    await add_category(legacy_conn, "Dairy", None)
    await add_category(legacy_conn, "Bakery", 1)
    await add_category(legacy_conn, "Fruit", None)

    await run_import(db, legacy_conn, owner)

    categories = await categories_of(db, await only_board(db))
    assert [c.name for c in categories] == ["Bakery", "Dairy", "Fruit"]
    assert [c.position for c in categories] == [0, 1, 2]


async def test_colliding_category_names_are_renamed(db, legacy_conn, owner):
    # v1 told names apart by case and by their tail; v2 does not.
    await add_category(legacy_conn, "Milk", 1)
    await add_category(legacy_conn, "milk", 2)
    await add_category(legacy_conn, "MILK", 3)
    await add_category(legacy_conn, "L" * 120 + "a", 4)
    await add_category(legacy_conn, "L" * 120 + "b", 5)
    await add_category(legacy_conn, "L" * 125, 6)

    report = await run_import(db, legacy_conn, owner)

    assert report.categories == 6
    assert report.renamed_categories == [
        "milk",
        "MILK",
        "L" * 120 + "a",
        "L" * 120 + "b",
        "L" * 125,
    ]
    names = [c.name for c in await categories_of(db, await only_board(db))]
    assert names == [
        "Milk",
        "milk (2)",
        "MILK (3)",
        "L" * 120,
        "L" * 116 + " (2)",  # the suffix fits in the 120 characters
        "L" * 116 + " (3)",
    ]


async def test_blank_names_abort_the_import(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    task_id = await add_task(legacy_conn, "   ", dairy)

    with pytest.raises(ValidationFailed, match=f"task {task_id}"):
        await import_legacy(db, legacy_conn, schema=SCHEMA, owner=owner)

    assert await board_count(db) == 0


async def test_board_name_and_each_import_makes_a_new_board(db, legacy_conn, owner):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(legacy_conn, "Milk", dairy)

    await run_import(db, legacy_conn, owner, board_name="  Old list ")
    await run_import(db, legacy_conn, owner)

    boards = list(await db.scalars(select(Board).order_by(Board.id)))
    assert [b.name for b in boards] == ["Old list", "Imported from v1"]
    assert await db.scalar(select(func.count()).select_from(Item)) == 2


async def test_invalid_board_name_rejected(db, legacy_conn, owner):
    with pytest.raises(ValidationFailed, match="Board name"):
        await import_legacy(db, legacy_conn, schema=SCHEMA, owner=owner, board_name="x" * 81)

    assert await board_count(db) == 0


async def test_only_the_named_schema_is_read(db, legacy_conn, owner):
    other = "legacy_other"
    await legacy_conn.execute(f"CREATE SCHEMA {other}")
    try:
        await legacy_conn.execute(
            LEGACY_DDL.replace(f"CREATE SCHEMA {SCHEMA};", "").replace(SCHEMA, other)
        )
        await legacy_conn.execute(f"INSERT INTO {other}.categories (name) VALUES ('Elsewhere')")
        await add_category(legacy_conn, "Dairy", 1)

        await run_import(db, legacy_conn, owner)

        names = [c.name for c in await categories_of(db, await only_board(db))]
        assert names == ["Dairy"]
    finally:
        await legacy_conn.execute(f"DROP SCHEMA {other} CASCADE")


# --- the command line ----------------------------------------------------------------------


async def import_command(legacy_dsn: str, *args: str) -> int:
    # main runs its own event loop (asyncio.run), so it cannot run in the test's.
    return await asyncio.to_thread(
        main, ["import-legacy", "--legacy-dsn", legacy_dsn, "--schema", SCHEMA, *args]
    )


async def test_cli_imports_and_prints_a_report(db, legacy_conn, owner, cli_env, legacy_dsn, capsys):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_category(legacy_conn, "X" * 130, 2)
    await add_task(legacy_conn, "Milk", dairy)
    await add_task(legacy_conn, "Batteries")

    code = await import_command(
        legacy_dsn, "--owner-email", " Dave@Example.com ", "--board-name", "Old list"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert 'Imported 3 categories and 2 items into the new board "Old list"' in out
    assert "dave@example.com" in out
    assert "1 item had no category in v1" in out
    assert "X" * 130 in out  # the renamed category
    board = await only_board(db)
    assert board.name == "Old list"
    assert board.owner_id == owner.id
    assert len(await categories_of(db, board)) == 3


async def test_cli_legacy_tz_option(db, legacy_conn, owner, cli_env, legacy_dsn):
    dairy = await add_category(legacy_conn, "Dairy", 1)
    await add_task(legacy_conn, "Milk", dairy, created_at=SUMMER)

    assert (
        await import_command(legacy_dsn, "--owner-email", "dave@example.com", "--legacy-tz", "UTC")
        == 0
    )

    assert (await db.scalar(select(Item))).created_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--owner-email", "nobody@example.com"], "No account with the email nobody@example.com"),
        (["--owner-email", "dave@example.com", "--legacy-tz", "Mars/Olympus"], "time zone"),
        (["--owner-email", "dave@example.com", "--board-name", " "], "Board name"),
    ],
)
async def test_cli_refuses_bad_arguments(
    db, legacy_conn, owner, cli_env, legacy_dsn, capsys, extra, message
):
    code = await import_command(legacy_dsn, *extra)

    assert code == 1
    assert message in capsys.readouterr().err
    assert await board_count(db) == 0


async def test_cli_refuses_a_bad_schema_name(db, legacy_conn, owner, cli_env, legacy_dsn, capsys):
    args = ["import-legacy", "--legacy-dsn", legacy_dsn, "--schema", "prod; drop table x"]

    code = await asyncio.to_thread(main, [*args, "--owner-email", "dave@example.com"])

    assert code == 1
    assert "schema" in capsys.readouterr().err
    assert await board_count(db) == 0


async def test_cli_reports_a_legacy_database_error(
    db, legacy_conn, owner, cli_env, legacy_dsn, capsys
):
    args = ["import-legacy", "--legacy-dsn", legacy_dsn, "--schema", "no_such_schema"]

    code = await asyncio.to_thread(main, [*args, "--owner-email", "dave@example.com"])

    assert code == 1
    assert "legacy database" in capsys.readouterr().err
    assert await board_count(db) == 0


async def test_cli_reports_a_malformed_legacy_dsn(db, owner, cli_env, capsys):
    args = ["import-legacy", "--legacy-dsn", "postgresql://u:p@localhost:notaport/db"]

    code = await asyncio.to_thread(
        main, [*args, "--schema", SCHEMA, "--owner-email", "dave@example.com"]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("error: Could not connect to the legacy database: ")
    assert await board_count(db) == 0


def test_import_report_names_the_category_name_limit(monkeypatch):
    """The report says how far names are cut by the limit the app has, not by a number of its own."""
    monkeypatch.setattr("easytodo.cli.CATEGORY_NAME_MAX_LENGTH", 80)

    report = _describe(ImportReport(1, 0, ["X"], 0), "Old list", "dave@example.com")

    assert "cut to 80 characters" in report
