"""Sorting a category (``prefs``): the "⇅" menu of every category, for every member. Apart from
the content routes (``content.routes``), whose every write needs the right to edit: a viewer may
sort, because the order is theirs alone."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from easytodo.auth.account_routes import SORT_INVALID
from easytodo.auth.deps import CurrentUser, DbSession, FormField
from easytodo.content.prefs import reset_category_sort, set_category_sort
from easytodo.content.routes import _current_category
from easytodo.errors import ValidationFailed
from easytodo.models import SORT_FIELDS

router = APIRouter()

_DIRECTIONS = ("asc", "desc")


@router.post("/categories/{category_id}/sort")
async def sort(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    category_id: int,
    sort_field: FormField = "",
    sort_direction: FormField = "",
    sort_open_first: FormField = "",  # "true" or "false"; left out: false
) -> Response:
    # A str, not a bool, as for the toggle: FastAPI reads a missing bool field as False.
    if (
        sort_field not in SORT_FIELDS
        or sort_direction not in _DIRECTIONS
        or sort_open_first not in ("", "true", "false")
    ):
        raise ValidationFailed(SORT_INVALID)
    await set_category_sort(
        db,
        category_id,
        user,
        field=sort_field,
        descending=sort_direction == "desc",
        open_first=sort_open_first == "true",
    )
    return await _current_category(request, db, user, category_id)


@router.post("/categories/{category_id}/sort/reset")
async def reset(request: Request, db: DbSession, user: CurrentUser, category_id: int) -> Response:
    await reset_category_sort(db, category_id, user)
    return await _current_category(request, db, user, category_id)
