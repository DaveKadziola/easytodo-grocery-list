import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

from fastapi import Request
from sqlalchemy import ColumnElement, DateTime, MetaData, collate, func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeEngine

from easytodo.config import Settings

logger = logging.getLogger(__name__)

# The ICU collation that sorts names in Polish alphabetical order (Ł after L, Ż after Z). The
# default collation of a database is not that: on postgres:17-alpine (musl) it is code-point
# order, which puts every Polish letter after Z. ICU collations ship with PostgreSQL, one per
# locale, in a UTF8 database; they are not created here (no DDL: the role may not own the schema).
NAME_COLLATION = "pl-x-icu"


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    # Every timestamp is timestamptz.
    type_annotation_map: ClassVar[dict[type, TypeEngine[Any]]] = {datetime: DateTime(timezone=True)}


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(), pool_size=10, pool_pre_ping=True
    )


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request from ``app.state.sessionmaker``."""
    async with request.app.state.sessionmaker() as session:
        yield session


async def detect_name_collation(engine: AsyncEngine) -> str | None:
    """``NAME_COLLATION`` if the database knows it, else None (a database that is not UTF8, a
    PostgreSQL built without ICU): names are then sorted in the database's own order, as before.
    Never an error: a collation is not worth a failed start. A warning says why it fell back."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"""SELECT 'a' COLLATE "{NAME_COLLATION}" < 'b'"""))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning(
            "Names are sorted in the database's own order, not in Polish alphabetical order: "
            "the collation %s is not available (%s). Use a UTF8 database, see the README.",
            NAME_COLLATION,
            type(exc).__name__,
        )
        return None
    return NAME_COLLATION


# What the app found at start-up (``use_name_collation``); the statements that sort names read it
# through ``name_collation``. It is a property of the database, the same for every request.
_name_collation: str | None = None


def use_name_collation(collation: str | None) -> None:
    """Sort names in ``collation`` (``detect_name_collation``'s answer) from now on."""
    global _name_collation
    _name_collation = collation


def name_collation() -> str | None:
    return _name_collation


def sorted_by_name(column: ColumnElement, collation: str | None) -> ColumnElement:
    """``column`` as names are ordered by: lower case, in the order of ``collation`` if there is
    one. (The statements are built once for each possible collation; see ``boards.service``.)"""
    lowered = func.lower(column)
    return lowered if collation is None else collate(lowered, collation)
