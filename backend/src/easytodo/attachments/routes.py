"""Attachments: adding and deleting them over HTMX (the answer is the item's details again), and
the files themselves, to anyone who may see the item.

Every file goes through the app, so a board's access rules apply to it. Browsers get a file under
a policy of its own (``FILE_CSP``: no script, a sandbox) and as ``nosniff``, so a text file that
is really HTML stays text, and the name is only ever sent percent-encoded.
"""

import asyncio
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.attachments.service import (
    ATTACHMENT_GONE,
    add_attachment,
    delete_attachment,
    load_attachment,
    read_text,
    render_markdown,
)
from easytodo.attachments.sniff import HEIC, download_name
from easytodo.attachments.storage import AttachmentStore
from easytodo.auth.deps import CurrentUser, DbSession
from easytodo.boards.permissions import Action
from easytodo.content.service import get_item_details
from easytodo.errors import NotFound
from easytodo.models import Attachment, User
from easytodo.web import render

# What a browser may do with a file that is shown on its own: not run anything, load nothing but
# its own pictures. The sandbox makes a page that slipped through (HTML called .txt) a page with
# no script and no access to the app's cookies.
FILE_CSP = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; sandbox"
)
# A PDF is shown by the browser's viewer, a plug-in that a sandbox would block.
PDF_CSP = (
    "default-src 'none'; object-src 'self'; img-src 'self'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'"
)

router = APIRouter()


def _store(request: Request) -> AttachmentStore:
    return request.app.state.attachment_store


async def _current_details(
    request: Request, db: AsyncSession, user: User, item_id: int
) -> Response:
    """The item's details fragment (``#item-details``), as ``content.routes`` answers with it
    (rendered here: that module imports this one)."""
    details = await get_item_details(db, item_id, user)
    return render(request, "boards/_item_details.html", {"details": details})


def _stat_of(path: Path) -> os.stat_result:
    """``path``'s status, or ``NotFound``: the row is there, the file is not (it was lost, or
    the volume is another one)."""
    try:
        return path.stat()
    except FileNotFoundError:
        raise NotFound(ATTACHMENT_GONE) from None


def _already_has(request: Request, file: FileResponse) -> bool:
    """Whether the browser sent the ETag of this very file (``If-None-Match``)."""
    sent = request.headers.get("if-none-match", "").strip()
    tags = [tag.strip().removeprefix("W/") for tag in sent.split(",")]
    return sent == "*" or file.headers["etag"] in tags


def _served(
    request: Request,
    path: Path,
    media_type: str,
    csp: str,
    *,
    filename: str | None = None,
    download: bool = False,
) -> Response:
    """The file at ``path``, with Range and ETag support (a browser that has it gets a 304). With
    a ``filename`` the browser is told to show it under that name or, with ``download``, to save
    it; the name goes in ``filename*`` alone, percent-encoded (RFC 5987): no byte of it can end a
    header or a quoted string."""
    headers = {
        "Content-Security-Policy": csp,
        # Revalidated on every use, so that a member who was removed from the board is not served
        # from the browser's cache (the ETag keeps that cheap).
        "Cache-Control": "private, no-cache",
    }
    if filename is not None:
        disposition = "attachment" if download else "inline"
        headers["Content-Disposition"] = (
            f"{disposition}; filename*=utf-8''{quote(filename, safe='')}"
        )
    file = FileResponse(path, media_type=media_type, stat_result=_stat_of(path), headers=headers)
    if not _already_has(request, file):
        return file
    # A 304 updates the headers that the browser keeps with its copy: it carries the file's own
    # policy (the sandbox, or the PDF viewer's) and name too, or the copy would lose them.
    names = ("ETag", "Last-Modified", *headers)
    return Response(status_code=304, headers={name: file.headers[name] for name in names})


@router.post("/items/{item_id}/attachments")
async def new_attachment(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    item_id: int,
    # Not required: a form without the file would be a 422 before the user's role is looked at;
    # the service answers a missing file after it.
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    await add_attachment(db, item_id, user, file, request.app.state.settings, _store(request))
    return await _current_details(request, db, user, item_id)


@router.delete("/attachments/{attachment_id}")
async def remove_attachment(
    request: Request, db: DbSession, user: CurrentUser, attachment_id: int
) -> Response:
    item_id = await delete_attachment(db, attachment_id, user, _store(request))
    return await _current_details(request, db, user, item_id)


async def _readable(db: AsyncSession, user: User, attachment_id: int) -> Attachment:
    attachment, _, _ = await load_attachment(db, attachment_id, user, Action.VIEW)
    return attachment


@router.get("/attachments/{attachment_id}")
async def view_attachment(
    request: Request, db: DbSession, user: CurrentUser, attachment_id: int
) -> Response:
    """A page that shows the file: the picture, the text, or Markdown rendered. A PDF is the
    browser's to show: it goes straight to the file."""
    attachment, item, _ = await load_attachment(db, attachment_id, user, Action.VIEW)
    if attachment.kind == "pdf":
        return RedirectResponse(f"/attachments/{attachment.id}/file", status_code=303)
    store = _store(request)
    heic = attachment.content_type == HEIC  # shown as its JPEG preview
    context: dict = {"attachment": attachment, "board_id": item.board_id, "heic": heic}
    _stat_of(
        store.preview_path(attachment.storage_key) if heic else store.path(attachment.storage_key)
    )
    if attachment.kind in ("text", "markdown"):
        try:
            text = await asyncio.to_thread(read_text, store, attachment)
            if attachment.kind == "markdown":
                context["markdown"] = await asyncio.to_thread(render_markdown, text)
            else:
                context["text"] = text
        except FileNotFoundError:
            raise NotFound(ATTACHMENT_GONE) from None
    return render(request, "attachments/view.html", context)


@router.get("/attachments/{attachment_id}/file")
async def attachment_file(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    attachment_id: int,
    download: bool = False,
) -> Response:
    """The file as it was uploaded (text and Markdown as plain text), shown or, with
    ``?download=1``, saved."""
    attachment = await _readable(db, user, attachment_id)
    if attachment.kind == "pdf":
        media_type, csp = "application/pdf", PDF_CSP
    elif attachment.kind == "image":
        media_type, csp = attachment.content_type, FILE_CSP
    else:  # whatever the text is (HTML, say), it is shown as text
        media_type, csp = "text/plain; charset=utf-8", FILE_CSP
    return _served(
        request,
        _store(request).path(attachment.storage_key),
        media_type,
        csp,
        filename=download_name(attachment.filename, attachment.content_type)
        if download
        else attachment.filename,
        download=download,
    )


@router.get("/attachments/{attachment_id}/thumb")
async def attachment_thumb(
    request: Request, db: DbSession, user: CurrentUser, attachment_id: int
) -> Response:
    """The thumbnail (WebP) of an image."""
    attachment = await _readable(db, user, attachment_id)
    if attachment.kind != "image":
        raise NotFound(ATTACHMENT_GONE)
    return _served(
        request,
        _store(request).thumb_path(attachment.storage_key),
        "image/webp",
        FILE_CSP,
    )


@router.get("/attachments/{attachment_id}/preview")
async def attachment_preview(
    request: Request, db: DbSession, user: CurrentUser, attachment_id: int
) -> Response:
    """The JPEG that a HEIC photo is shown as; no other file has one."""
    attachment = await _readable(db, user, attachment_id)
    if attachment.kind != "image" or attachment.content_type != HEIC:
        raise NotFound(ATTACHMENT_GONE)
    return _served(
        request,
        _store(request).preview_path(attachment.storage_key),
        "image/jpeg",
        FILE_CSP,
    )
