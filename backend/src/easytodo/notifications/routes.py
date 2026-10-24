"""The bell: its badge (the number of unread notifications), its panel (the latest ones) and
"read", which clears the badge. Each answers with the piece of the page it is swapped into
(``static/js/notifications.js`` asks; the header only holds an empty badge that loads itself, so
no page is rendered with a database statement for the bell)."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from easytodo.auth.deps import CurrentUser, DbSession
from easytodo.notifications.service import RETENTION, mark_all_read, recent, unread_count
from easytodo.web import render

router = APIRouter(prefix="/notifications")


def _badge(request: Request, count: int) -> Response:
    return render(request, "notifications/_badge.html", {"count": count})


@router.get("/badge")
async def badge(request: Request, db: DbSession, user: CurrentUser) -> Response:
    return _badge(request, await unread_count(db, user))


@router.get("/panel")
async def panel(request: Request, db: DbSession, user: CurrentUser) -> Response:
    context = {"notifications": await recent(db, user), "retention_days": RETENTION.days}
    return render(request, "notifications/_panel.html", context)


@router.post("/read")
async def read(request: Request, db: DbSession, user: CurrentUser) -> Response:
    """Mark everything read: the badge it answers with is the empty one, as the unread are
    none (a notification that arrives meanwhile tells every page, this one too)."""
    await mark_all_read(db, user)
    return _badge(request, 0)
