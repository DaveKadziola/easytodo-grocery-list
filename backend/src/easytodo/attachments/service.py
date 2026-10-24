"""Files attached to an item: images, PDFs, plain text and Markdown.

Like the other content services, every function finds what it changes through the user's own board
memberships (an attachment of a deleted item, or in an archived category or board, answers
``NotFound``; a role that may not edit gets ``Forbidden``) and commits the change together with
its board event, which names the item (a page with it open fetches its details again, and the
category: its tile shows how many files there are).

What a file is, is decided by its content (``sniff``), never by what the browser says; the bytes
go to the ``AttachmentStore``, outside the database, under a random key. An upload that is refused,
or that fails half way, leaves nothing on disk and nothing in the database.
"""

import asyncio
import codecs
import logging
import secrets
import threading
import unicodedata
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

import pillow_heif
from fastapi import UploadFile
from markdown_it import MarkdownIt
from markupsafe import Markup
from PIL import Image, ImageOps
from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.attachments.sniff import HEAD_SIZE, HEIC, detect
from easytodo.attachments.storage import AttachmentStore
from easytodo.boards.permissions import (
    Action,
    MemberLookup,
    RowLock,
    is_possible_id,
    load_for_member,
    member_lookup,
)
from easytodo.config import Settings
from easytodo.content.service import _category_changed, item_child_query, load_item
from easytodo.errors import Conflict, NotFound, PayloadTooLarge, ValidationFailed
from easytodo.models import Attachment, BoardRole, Item, User
from easytodo.realtime.events import BoardEvent, commit_and_publish

logger = logging.getLogger(__name__)

TOO_LARGE = "Files can be at most {mb} MB."
TOO_MANY = "A task can have at most {n} attachments."
STORAGE_FULL = "Attachment storage is full."
NOT_ALLOWED = "Only images, PDF, .txt and .md files can be attached."
NOT_UTF8 = "Text files must be saved as UTF-8."
BROKEN_IMAGE = "This image could not be read."
NO_FILE = "Choose a file to attach."
ATTACHMENT_GONE = "This attachment no longer exists."

FILENAME_MAX_LENGTH = 255  # attachments.filename is String(255)
THUMB_SIZE = (480, 480)  # the tile in the item's details
PREVIEW_SIZE = (2048, 2048)  # a HEIC photo, as a JPEG that browsers can show
# How many pixels a picture may have, by Pillow's name of its format, checked from the header
# before anything is decoded (the same refusal as the pixel limit of Pillow itself). Decoding
# holds the whole picture, and the peak depends on the format, not on the file: a WebP of 38 bytes
# with 9449 x 9449 pixels took 1.3 GB, a 64 MP panorama HEIC killed a 512 MiB container. 48 MP
# phone photos fit. A JPEG has no budget: it is decoded at a fraction of its size (``draft``), so
# the limit of Pillow is enough. docs/deployment.md ("Memory for photos") has the peaks.
WEBP_MAX_PIXELS = 20_000_000
PNG_MAX_PIXELS = 36_000_000
GIF_MAX_PIXELS = 36_000_000
HEIF_MAX_PIXELS = 50_000_000
PIXEL_BUDGETS = {
    "WEBP": WEBP_MAX_PIXELS,
    "PNG": PNG_MAX_PIXELS,
    "GIF": GIF_MAX_PIXELS,
    "HEIF": HEIF_MAX_PIXELS,
}
# A Markdown file is rendered up to this size (in bytes); a larger one is shown as plain text.
# Rendering takes longer than the file grows: 1 MB of "[" took half a minute.
MARKDOWN_RENDER_LIMIT = 256 * 1024
_MEGABYTE = 1024 * 1024
_CHUNK = 1024 * 1024

pillow_heif.register_heif_opener()  # Image.open reads HEIC from now on

# The bytes of all attachments (SUM of a bigint is a numeric, which asyncpg returns as a Decimal),
# as a subquery for the statements that need them: the instance's quota here, the admin's statistics.
total_bytes = select(
    func.coalesce(cast(func.sum(Attachment.size_bytes), BigInteger), 0)
).scalar_subquery()
# How many files the item has and how many bytes there are in all: one statement.
_USAGE = select(
    select(func.count())
    .where(Attachment.item_id == bindparam("item_id", type_=BigInteger))
    .scalar_subquery()
    .label("files"),
    total_bytes.label("bytes"),
)


@cache
def _attachment_lookup(action: Action, lock: RowLock | None) -> MemberLookup:
    """The statements of ``load_attachment`` for one action and lock, built once."""
    query = item_child_query(Attachment, "attachment_id")
    return member_lookup(query, action, lock=lock, of=Attachment)


async def load_attachment(
    db: AsyncSession,
    attachment_id: int,
    user: User,
    action: Action,
    *,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
) -> tuple[Attachment, Item, BoardRole]:
    """The attachment, its item and ``user``'s role, in one query joined with the item, its active
    category, the active board and ``board_members``. An attachment of a deleted item or one in
    an archived category or board is not found.

    For a change (any action but VIEW) the attachment's row stays locked in mode ``lock`` until
    the commit, so a delete from another device that got there first makes this ``NotFound``. A
    member whose role does not allow the change (a viewer) takes no lock and waits for nobody
    (see ``load_for_member``). The item is read, not locked.
    """
    if not is_possible_id(attachment_id):
        raise NotFound(ATTACHMENT_GONE)
    row_lock = None if action is Action.VIEW else lock
    attachment, item, role = await load_for_member(
        db,
        _attachment_lookup(action, row_lock),
        ATTACHMENT_GONE,
        {"attachment_id": attachment_id, "user_id": user.id},
    )
    return attachment, item, role


def too_large(settings: Settings) -> PayloadTooLarge:
    mb = round(settings.attachment_max_bytes / _MEGABYTE, 1)
    return PayloadTooLarge(TOO_LARGE.format(mb=f"{mb:g}"))


def clean_filename(name: str) -> str:
    """``name`` as it is kept, for showing only (the file is stored under a key): without control
    characters or format characters (the right-to-left override that makes "gpj.exe" read as
    "exe.jpg", zero-width ones), at most 255 characters long, cut in the middle of the name rather
    than of its extension. A "/" stays: nothing ever turns the name into a path."""
    name = "".join(char for char in name if unicodedata.category(char) not in ("Cc", "Cf")).strip()
    if len(name) > FILENAME_MAX_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and len(extension) < 20:
            name = stem[: FILENAME_MAX_LENGTH - len(extension) - 1] + dot + extension
        else:
            name = name[:FILENAME_MAX_LENGTH]
    return name or "file"


def _text_problem(source: BinaryIO) -> str | None:
    """Why the rest of ``source`` is not a text file, or None if it is one: ``NOT_UTF8`` for what
    UTF-8 cannot read (a file saved in cp1250 or UTF-16, say), ``NOT_ALLOWED`` for a NUL
    character, which no text has."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while chunk := source.read(_CHUNK):
            if "\x00" in decoder.decode(chunk):
                return NOT_ALLOWED
        return NOT_ALLOWED if "\x00" in decoder.decode(b"", final=True) else None
    except UnicodeDecodeError:
        return NOT_UTF8


# One thread each for decoding pictures and for rendering Markdown, the way argon2 has its own
# (``security.passwords``): not ``asyncio.to_thread``, whose executor has up to 32 threads that
# other work waits for. Decoding one picture takes up to several hundred MB, so there is one at a
# time per worker; a render can take seconds of CPU, so two cannot hold more than one thread.
_pools: dict[str, ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()


def _pool(name: str) -> ThreadPoolExecutor:
    """The single thread called ``name``, started on first use, not at import. One for the whole
    process, however many apps it runs (the tests start one per test), so it is never shut down:
    its idle thread ends with the interpreter."""
    with _pools_lock:
        if name not in _pools:
            _pools[name] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        return _pools[name]


async def run_in_pool[T](name: str, function: Callable[..., T], *args: object) -> T:
    """``function(*args)`` on the thread ``name`` (see ``_pool``)."""
    return await asyncio.get_running_loop().run_in_executor(_pool(name), function, *args)


# ``warnings.catch_warnings`` changes the warning filters of the whole process: two threads in it
# at once could each put back the other's state. Only the opening of a picture is inside.
_WARNINGS_LOCK = threading.Lock()


def _open(path: Path) -> Image.Image:
    """``Image.open``, but a picture with more than ``Image.MAX_IMAGE_PIXELS`` pixels is an error
    at once: Pillow itself only warns (twice) up to twice the limit and refuses above."""
    with _WARNINGS_LOCK, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return Image.open(path)


def _check_budget(picture: Image.Image) -> None:
    """``Image.DecompressionBombError`` for a picture with more pixels than its format's budget
    (``PIXEL_BUDGETS``). The size is in the header: nothing has been decoded yet."""
    budget = PIXEL_BUDGETS.get(picture.format or "")
    pixels = picture.width * picture.height
    if budget is not None and pixels > budget:
        raise Image.DecompressionBombError(
            f"{picture.format} picture of {picture.width} x {picture.height} = {pixels} pixels "
            f"(at most {budget})"
        )


def _decode_images(path: Path, heic: bool) -> tuple[Image.Image | None, Image.Image]:
    """The JPEG preview of a HEIC photo (None for any other picture) and the thumbnail of the
    picture stored at ``path``, in memory. ``ValidationFailed(BROKEN_IMAGE)`` for anything Pillow
    cannot read (reading and turning only: writing the result is not this)."""
    try:
        with _open(path) as probe:
            _check_budget(probe)
            probe.verify()  # the file's structure; it cannot be used afterwards
        with _open(path) as picture:
            if heic:
                picture.thumbnail(PREVIEW_SIZE)
                preview = ImageOps.exif_transpose(picture).convert("RGB")
                thumb = preview.copy()
            else:
                picture.thumbnail(THUMB_SIZE)  # first: the turn below then costs next to nothing
                preview, thumb = None, ImageOps.exif_transpose(picture)
            thumb.thumbnail(THUMB_SIZE)
            if thumb.mode not in ("RGB", "RGBA"):
                thumb = thumb.convert("RGBA" if thumb.has_transparency_data else "RGB")
            return preview, thumb
    except Exception as exc:  # Pillow's decoders raise all sorts of errors on a corrupt file
        logger.info("An image could not be read: %r", exc)
        raise ValidationFailed(BROKEN_IMAGE) from exc


def _make_images(store: AttachmentStore, key: str, heic: bool) -> None:
    """Read the image stored as ``key`` and write its thumbnail beside it, and for a HEIC photo
    its JPEG preview. A file that cannot be read is ``ValidationFailed(BROKEN_IMAGE)``; one that
    cannot be written (a full disk) is the error of the write, a server error."""
    preview, thumb = _decode_images(store.path(key), heic)
    if preview is not None:
        preview.save(store.preview_path(key), "JPEG", quality=85)
    thumb.save(store.thumb_path(key), "WEBP", quality=80)


def _size_of(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def _item_changed(item: Item) -> BoardEvent:
    return _category_changed(item.board_id, item.category_id, item_ids=(item.id,))


async def add_attachment(
    db: AsyncSession,
    item_id: int,
    user: User,
    upload: UploadFile | None,
    settings: Settings,
    store: AttachmentStore,
) -> Attachment:
    """Attach the uploaded file to the item. In this order: ``Forbidden``/``NotFound`` for the
    item, ``ValidationFailed`` without a file, ``PayloadTooLarge`` above the size limit, a
    ``Conflict`` when the item has its fill of files or the instance its quota, and
    ``ValidationFailed`` for a type that may not be attached, a text file that is not UTF-8 or an
    image Pillow cannot read or that has more pixels than its format's budget. Only then is
    anything written."""
    # The lock on the item (FOR NO KEY UPDATE, as every change to it takes) serializes the files
    # added at the same time, so that each counts the others. A tick or a rename of the same
    # item waits for it while the file is written and thumbnailed (accepted).
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    if upload is None or not upload.filename:
        raise ValidationFailed(NO_FILE)
    size = _size_of(upload)
    if size > settings.attachment_max_bytes:
        raise too_large(settings)
    files, stored = (await db.execute(_USAGE, {"item_id": item.id})).one()
    if files >= settings.attachments_per_item:
        raise Conflict(TOO_MANY.format(n=settings.attachments_per_item))
    if stored + size > settings.attachments_total_bytes:
        raise Conflict(STORAGE_FULL)
    filename = clean_filename(upload.filename)
    detected = detect(await upload.read(HEAD_SIZE), filename)
    await upload.seek(0)
    if detected is None:
        raise ValidationFailed(NOT_ALLOWED)
    if detected.kind in ("text", "markdown"):
        if problem := await asyncio.to_thread(_text_problem, upload.file):
            raise ValidationFailed(problem)
        await upload.seek(0)

    key = secrets.token_hex(16)
    attachment = Attachment(
        item_id=item.id,
        filename=filename,
        kind=detected.kind,
        content_type=detected.content_type,
        size_bytes=size,
        storage_key=key,
        uploaded_by=user.id,
    )
    try:
        await store.save(key, upload.file)
        if detected.kind == "image":
            await run_in_pool("images", _make_images, store, key, detected.content_type == HEIC)
        db.add(attachment)
        await commit_and_publish(db, _item_changed(item))
    except BaseException:
        store.delete(key)  # nothing is left of a file that did not get in
        raise
    return attachment


async def delete_attachment(
    db: AsyncSession, attachment_id: int, user: User, store: AttachmentStore
) -> int:
    """Delete the attachment and its files; returns the id of its item. The files go after the
    commit: a delete that fails leaves them in place, and one that is lost between the two steps
    leaves files without a row, which the purge removes."""
    attachment, item, _ = await load_attachment(
        db, attachment_id, user, Action.EDIT_CONTENT, lock=RowLock.UPDATE
    )
    key = attachment.storage_key
    await db.delete(attachment)
    await commit_and_publish(db, _item_changed(item))
    store.delete(key)
    return item.id


def _safe_link(url: str) -> bool:
    """Only a link to a web page, a mail address or a place on this site is one: not
    ``javascript:`` or ``data:`` or whatever else. (``urlsplit`` reads the scheme as a browser
    does: any case, no tabs or line breaks inside, nothing but spaces before it.)"""
    try:
        return urlsplit(url).scheme in {"", "http", "https", "mailto"}
    except ValueError:
        return False


# Raw HTML in the text is shown as text; no images (a picture would be fetched from wherever it
# says, which tells that site who reads the file): `![x](url)` is a plain link instead.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")
_MARKDOWN.validateLink = _safe_link


def render_markdown(text: str) -> Markup:
    """``text`` as HTML that is safe to put in a page: Markdown's own tags and the links that
    ``_safe_link`` lets through, everything else escaped."""
    return Markup(_MARKDOWN.render(text))


def read_text(store: AttachmentStore, attachment: Attachment) -> str:
    """The text of a text or Markdown attachment (blocking: run it in a thread). A leading byte
    order mark is dropped; what is not UTF-8 any more is replaced, not an error."""
    return store.path(attachment.storage_key).read_bytes().decode("utf-8-sig", errors="replace")
