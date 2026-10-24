"""How a member sorts a category: their own choice, kept per user and category and never shared
with the board, so any member may make it (a viewer too) and no event is sent. Without one the
account's default applies (see ``boards.service.select_item_views``)."""

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.permissions import Action
from easytodo.boards.service import select_sort_pref
from easytodo.content.service import _FOREIGN_KEY_VIOLATION, CATEGORY_GONE, load_category
from easytodo.errors import NotFound
from easytodo.models import CategorySortPref, User
from easytodo.realtime.events import commit_and_publish


async def get_category_sort(
    db: AsyncSession, category_id: int, user: User
) -> CategorySortPref | None:
    """``user``'s own sort row for the category, if they have made one."""
    category, _ = await load_category(db, category_id, user, Action.VIEW)
    return await select_sort_pref(db, user, category.id)


async def set_category_sort(
    db: AsyncSession,
    category_id: int,
    user: User,
    *,
    field: str,
    descending: bool,
    open_first: bool,
) -> None:
    """Make ``user``'s order for the category what is given (one row per user and category)."""
    category, _ = await load_category(db, category_id, user, Action.VIEW)
    values = {"field": field, "descending": descending, "open_first": open_first}
    upsert = insert(CategorySortPref).values(user_id=user.id, category_id=category.id, **values)
    upsert = upsert.on_conflict_do_update(
        index_elements=["user_id", "category_id"], set_=values | {"updated_at": func.now()}
    )
    try:
        # A savepoint, as for a category's name: a failed write keeps the rest of the request.
        async with db.begin_nested():
            await db.execute(upsert)
    except IntegrityError as exc:
        # Reading a category takes no lock, so it can be deleted before the row is written.
        if getattr(exc.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION:
            raise NotFound(CATEGORY_GONE) from None
        raise
    await commit_and_publish(db)


async def reset_category_sort(db: AsyncSession, category_id: int, user: User) -> None:
    """Back to the account's default for the category (also when it already is)."""
    category, _ = await load_category(db, category_id, user, Action.VIEW)
    await db.execute(
        delete(CategorySortPref).where(
            CategorySortPref.user_id == user.id, CategorySortPref.category_id == category.id
        )
    )
    await commit_and_publish(db)
