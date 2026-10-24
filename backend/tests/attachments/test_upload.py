"""Attaching files to an item and deleting them: what is accepted, what is refused (and leaves
nothing behind), the limits, the thumbnails, the store of files, and the lookups and locks of the
service. Serving them is in test_serve.py."""

import asyncio
import errno
import io
import json
import re
import warnings

import httpx
import pytest
from conftest import _test_settings
from content.test_board_markup import EDIT_CONTROLS
from factories import create_attachment, create_item
from helpers import (
    NASTY,
    NASTY_ESCAPED,
    toast_message,
    wait_until_a_query_waits_for_a_lock,
)
from PIL import Image
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects import postgresql
from starlette.requests import ClientDisconnect

from attachments import samples
from attachments.samples import HX, multipart, upload
from easytodo.attachments import service as attachments
from easytodo.attachments.service import (
    ATTACHMENT_GONE,
    BROKEN_IMAGE,
    NO_FILE,
    NOT_ALLOWED,
    STORAGE_FULL,
    TOO_MANY,
    _attachment_lookup,
    clean_filename,
    delete_attachment,
    load_attachment,
)
from easytodo.attachments.storage import AttachmentStore
from easytodo.boards.permissions import Action, RowLock
from easytodo.content.service import _category_changed
from easytodo.errors import SERVER_ERROR
from easytodo.middleware import FORM_OVERHEAD, UploadSizeLimitMiddleware
from easytodo.models import Attachment, Category, User
from easytodo.security.csrf import CSRF_HEADER

MEGABYTE = 1024 * 1024
TOO_LARGE_10 = "Files can be at most 10 MB."


def files_in(store: AttachmentStore) -> list[str]:
    """Every file under the store's directory, thumbnails and previews too."""
    return sorted(
        str(path.relative_to(store.root)) for path in store.root.rglob("*") if path.is_file()
    )


async def attachment_rows(db) -> list[Attachment]:
    return list((await db.scalars(select(Attachment).order_by(Attachment.id))).all())


# ---- the store of files


def test_store_without_its_directory_has_no_keys(tmp_path):
    assert list(AttachmentStore(tmp_path / "missing").keys()) == []


async def test_store_keeps_a_file_under_its_key_with_the_thumbnail_and_preview_beside_it(tmp_path):
    store = AttachmentStore(tmp_path / "files")
    key = "ab" + "0" * 30

    await store.save(key, io.BytesIO(b"bytes"))

    assert store.path(key) == tmp_path / "files" / "ab" / key
    assert store.thumb_path(key) == tmp_path / "files" / "ab" / f"{key}.thumb.webp"
    assert store.preview_path(key) == tmp_path / "files" / "ab" / f"{key}.preview.jpg"
    assert store.path(key).read_bytes() == b"bytes"


async def test_store_saves_from_the_current_position(tmp_path):
    store = AttachmentStore(tmp_path)
    source = io.BytesIO(b"0123456789")
    source.seek(4)

    await store.save("c" * 32, source)

    assert store.path("c" * 32).read_bytes() == b"456789"


async def test_store_never_overwrites_a_file(tmp_path):
    store = AttachmentStore(tmp_path)
    await store.save("d" * 32, io.BytesIO(b"first"))

    with pytest.raises(FileExistsError):
        await store.save("d" * 32, io.BytesIO(b"second"))

    assert store.path("d" * 32).read_bytes() == b"first"


async def test_store_deletes_the_three_files_and_does_not_mind_missing_ones(tmp_path):
    store = AttachmentStore(tmp_path)
    key, other = "e" * 32, "f" * 32
    await store.save(key, io.BytesIO(b"main"))
    await store.save(other, io.BytesIO(b"other"))
    store.thumb_path(key).write_bytes(b"thumb")
    store.preview_path(key).write_bytes(b"preview")

    store.delete(key)
    store.delete(key)  # again: nothing is there, nothing is wrong
    store.delete("0" * 32)  # never existed

    assert list(store.keys()) == [other]
    assert not store.thumb_path(key).exists() and not store.preview_path(key).exists()


async def test_store_lists_only_main_files(tmp_path):
    store = AttachmentStore(tmp_path)
    keys = {"1" * 32, "2" * 32, "1a" + "3" * 30}
    for key in keys:
        await store.save(key, io.BytesIO(b"x"))
        store.thumb_path(key).write_bytes(b"x")
        store.preview_path(key).write_bytes(b"x")
    (tmp_path / "stray.txt").write_bytes(b"x")  # not in a directory of keys

    assert set(store.keys()) == keys


async def test_store_lists_the_derived_files_whose_file_is_missing(tmp_path):
    """What a cancelled upload leaves: the thumbnail and the preview of a file that is gone, which
    ``keys`` never names."""
    store = AttachmentStore(tmp_path)
    with_file, orphan, other = "1" * 32, "2" * 32, "2a" + "3" * 30
    await store.save(with_file, io.BytesIO(b"x"))
    store.thumb_path(with_file).write_bytes(b"x")
    for key in (orphan, other):
        store.thumb_path(key).parent.mkdir(exist_ok=True)
    store.thumb_path(orphan).write_bytes(b"x")
    store.preview_path(orphan).write_bytes(b"x")
    store.preview_path(other).write_bytes(b"x")
    (store.root / orphan[:2] / "stray.thumb.webp").write_bytes(b"x")  # not a key
    (tmp_path / "stray.thumb.webp").write_bytes(b"x")  # not in a directory of keys

    assert set(store.derived_orphans()) == {
        (orphan, store.thumb_path(orphan)),
        (orphan, store.preview_path(orphan)),
        (other, store.preview_path(other)),
    }
    assert list(AttachmentStore(tmp_path / "missing").derived_orphans()) == []


async def test_store_says_whether_it_was_the_one_that_deleted(tmp_path):
    store = AttachmentStore(tmp_path)
    key, orphan = "e" * 32, "f" * 32
    await store.save(key, io.BytesIO(b"main"))
    store.thumb_path(orphan).parent.mkdir()
    store.thumb_path(orphan).write_bytes(b"thumb")

    assert store.delete(key) is True
    assert store.delete(key) is False  # nothing was there: no error, and not this call's doing
    assert store.delete_derived(store.thumb_path(orphan)) is True
    assert store.delete_derived(store.thumb_path(orphan)) is False
    assert list(store.derived_orphans()) == []


# ---- accepted files


@pytest.mark.parametrize(
    ("filename", "content", "kind", "content_type", "thumb", "preview"),
    [
        pytest.param("photo.jpg", samples.jpeg(), "image", "image/jpeg", True, False, id="jpeg"),
        pytest.param("photo.png", samples.png(), "image", "image/png", True, False, id="png"),
        pytest.param("photo.webp", samples.webp(), "image", "image/webp", True, False, id="webp"),
        pytest.param("party.gif", samples.gif(), "image", "image/gif", True, False, id="gif"),
        pytest.param("IMG_0001.HEIC", samples.heic(), "image", "image/heic", True, True, id="heic"),
        pytest.param("receipt.pdf", samples.PDF, "pdf", "application/pdf", False, False, id="pdf"),
        pytest.param("note.txt", samples.TEXT, "text", "text/plain", False, False, id="txt"),
        pytest.param("plan.md", b"# Plan\n", "markdown", "text/markdown", False, False, id="md"),
        pytest.param(
            "plan.markdown", b"# Plan\n", "markdown", "text/markdown", False, False, id="markdown"
        ),
        pytest.param(
            "Zażółć.txt",
            "Zażółć gęślą jaźń\n".encode(),
            "text",
            "text/plain",
            False,
            False,
            id="polish",
        ),
        pytest.param("empty.txt", b"", "text", "text/plain", False, False, id="empty"),
        # The signature decides, not the name.
        pytest.param(
            "photo.txt", samples.png(), "image", "image/png", True, False, id="png-as-txt"
        ),
    ],
)
async def test_each_allowed_type_is_accepted(
    editor_client,
    item,
    category,
    board,
    db,
    store,
    published,
    filename,
    content,
    kind,
    content_type,
    thumb,
    preview,
):
    r = await editor_client.post(
        f"/items/{item.id}/attachments",
        files={"file": (filename, content, "application/octet-stream")},
        headers=HX | {"X-Client-Id": "tab-1"},
    )

    assert r.status_code == 200, r.headers.get("hx-trigger")
    (row,) = await attachment_rows(db)
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    assert (row.item_id, row.filename, row.kind, row.content_type, row.size_bytes) == (
        item.id,
        filename,
        kind,
        content_type,
        len(content),
    )
    assert row.uploaded_by == editor.id
    assert re.fullmatch(r"[0-9a-f]{32}", row.storage_key)
    assert store.path(row.storage_key).read_bytes() == content
    assert store.thumb_path(row.storage_key).is_file() is thumb
    assert store.preview_path(row.storage_key).is_file() is preview
    assert files_in(store) == sorted(
        [f"{row.storage_key[:2]}/{row.storage_key}"]
        + ([f"{row.storage_key[:2]}/{row.storage_key}.thumb.webp"] if thumb else [])
        + ([f"{row.storage_key[:2]}/{row.storage_key}.preview.jpg"] if preview else [])
    )
    assert f'data-attachment-id="{row.id}"' in r.text  # the answer is the item's details again
    event = _category_changed(board.id, category.id, item_ids=(item.id,))
    assert published == [((event,), "tab-1")]

    published.clear()
    r = await editor_client.delete(f"/attachments/{row.id}", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    assert "data-attachment-id" not in r.text and "No attachments yet." in r.text
    assert await attachment_rows(db) == []
    assert files_in(store) == []  # the main file, the thumbnail and the preview
    assert published == [((event,), "tab-1")]


async def test_a_second_upload_is_listed_after_the_first(editor_client, item, db):
    first = await upload(editor_client, item.id, "first.txt")
    second = await upload(editor_client, item.id, "second.txt")

    r = await editor_client.get(f"/items/{item.id}/details", headers=HX)

    assert second > first
    assert re.findall(r'data-attachment-id="(\d+)"', r.text) == [str(first), str(second)]


# ---- thumbnails and previews


def read_picture(path) -> Image.Image:
    picture = Image.open(path)
    picture.load()
    return picture


async def test_a_thumbnail_fits_in_480_px_and_the_original_is_kept(editor_client, item, db, store):
    original = samples.png((1000, 500))

    attachment_id = await upload(editor_client, item.id, "big.png", original, "image/png")

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    assert store.path(key).read_bytes() == original  # as it was uploaded, whatever its size
    thumb = read_picture(store.thumb_path(key))
    assert (thumb.format, thumb.size) == ("WEBP", (480, 240))
    assert not store.preview_path(key).exists()


async def test_a_small_picture_is_not_enlarged(editor_client, item, db, store):
    attachment_id = await upload(editor_client, item.id, "tiny.jpg", samples.jpeg((32, 24)))

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    assert read_picture(store.thumb_path(key)).size == (32, 24)


async def test_a_thumbnail_is_turned_upright_by_the_orientation_in_the_photo(
    editor_client, item, db, store
):
    attachment_id = await upload(
        editor_client, item.id, "phone.jpg", samples.jpeg_rotated_by_exif((40, 20))
    )

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    thumb = read_picture(store.thumb_path(key))
    assert thumb.size == (20, 40)  # shown upright, as the photo is
    assert not thumb.getexif()  # and carries nothing of the original's metadata


async def test_a_heic_photo_gets_a_jpeg_of_at_most_2048_px_and_a_thumbnail(
    editor_client, item, db, store
):
    photo = samples.heic((2400, 1200))

    attachment_id = await upload(editor_client, item.id, "IMG_0001.heic", photo, "image/heic")

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    assert store.path(key).read_bytes() == photo
    preview = read_picture(store.preview_path(key))
    assert (preview.format, preview.size) == ("JPEG", (2048, 1024))
    assert read_picture(store.thumb_path(key)).size == (480, 240)


@pytest.mark.parametrize("mode", ["P", "RGBA", "LA", "L", "1", "CMYK"])
async def test_pictures_in_any_mode_get_a_thumbnail(editor_client, item, db, store, mode):
    buffer = io.BytesIO()
    fmt = "JPEG" if mode == "CMYK" else "PNG"
    Image.new(mode, (50, 30)).save(buffer, format=fmt)

    attachment_id = await upload(editor_client, item.id, "mode.img", buffer.getvalue())

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    assert read_picture(store.thumb_path(key)).size == (50, 30)


async def test_the_thumbnail_of_an_animated_gif_is_its_first_frame(editor_client, item, db, store):
    frames = [Image.new("RGB", (40, 30), color) for color in ("red", "blue", "green")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=50)

    attachment_id = await upload(editor_client, item.id, "spin.gif", buffer.getvalue())

    key = (await db.get_one(Attachment, attachment_id)).storage_key
    assert read_picture(store.thumb_path(key)).size == (40, 30)
    assert store.path(key).read_bytes() == buffer.getvalue()  # the original keeps its frames


# ---- refused uploads


def chunked(boundary: str, filename: str, size: int):
    """A form with one file of ``size`` bytes, sent in 1 MB chunks and no Content-Length."""
    head, tail = multipart(boundary, filename, b"").split(b"\r\n--", 1)

    async def body():
        yield head + b"\r\n"
        sent = 0
        while sent < size:
            chunk = min(MEGABYTE, size - sent)
            yield b"x" * chunk
            sent += chunk
        yield b"\r\n--" + tail

    return body()


def nothing_left(rows, store):
    return rows == [] and files_in(store) == []


def refused(how, filename, content, status, message, id):
    return pytest.param(how, filename, content, status, message, id=id)


@pytest.mark.parametrize(
    ("how", "filename", "content", "status", "message"),
    [
        refused("declared", "big.txt", 11 * MEGABYTE, 413, TOO_LARGE_10, "11 MB with a length"),
        refused("chunked", "big.txt", 11 * MEGABYTE, 413, TOO_LARGE_10, "11 MB chunked"),
        refused("declared", "big.txt", 10 * MEGABYTE + 1, 413, TOO_LARGE_10, "10 MB and a byte"),
        refused("declared", "big.png", 11 * MEGABYTE, 413, TOO_LARGE_10, "11 MB image"),
        refused(
            "declared", "logo.png", samples.WINDOWS_PROGRAM, 422, NOT_ALLOWED, "program as png"
        ),
        refused("declared", "logo.svg", b"<svg><script/></svg>", 422, NOT_ALLOWED, "svg"),
        refused("declared", "page.html", b"<script>x</script>", 422, NOT_ALLOWED, "html"),
        refused("declared", "list.txt", "Zurück".encode("latin-1"), 422, NOT_ALLOWED, "latin-1"),
        refused("declared", "list.txt", b"Buy\x00milk", 422, NOT_ALLOWED, "text with a NUL"),
        refused("declared", "list.md", b"ok\n\xff", 422, NOT_ALLOWED, "markdown, bad UTF-8"),
        refused("declared", "list.txt", b"\xff\xfeB\x00u\x00", 422, NOT_ALLOWED, "utf-16 text"),
        refused(
            "declared", "half.png", samples.png((200, 200))[:60], 422, BROKEN_IMAGE, "half png"
        ),
        refused(
            "declared", "half.jpg", samples.jpeg((200, 200))[:300], 422, BROKEN_IMAGE, "half jpeg"
        ),
        refused("declared", "half.heic", samples.heic()[:80], 422, BROKEN_IMAGE, "half heic"),
        refused(
            "declared",
            "junk.png",
            b"\x89PNG\r\n\x1a\n" + b"junk" * 50,
            422,
            BROKEN_IMAGE,
            "junk png",
        ),
        refused("none", None, None, 422, NO_FILE, "no file field"),
        refused("declared", "", b"", 422, NO_FILE, "a file field without a file"),
    ],
)
async def test_rejected_uploads_leave_nothing_behind(  # Review Focus 5
    editor_client, item, db, store, published, how, filename, content, status, message
):
    url = f"/items/{item.id}/attachments"
    if how == "declared":
        if isinstance(content, int):
            content = b"x" * content
        r = await editor_client.post(url, files={"file": (filename, content)}, headers=HX)
    elif how == "chunked":
        boundary = "chunked-boundary"
        headers = HX | {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        r = await editor_client.post(
            url, content=chunked(boundary, filename, content), headers=headers
        )
    else:
        r = await editor_client.post(url, data={"note": "x"}, headers=HX)

    assert r.status_code == status
    assert toast_message(r) == message
    assert r.text == ""
    assert nothing_left(await attachment_rows(db), store)
    assert published == []


async def test_a_refused_upload_to_a_page_shows_the_error_page(editor_client, item, db, store):
    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("big.txt", b"x" * 11 * MEGABYTE)}
    )

    assert r.status_code == 413
    assert TOO_LARGE_10 in r.text and "413" in r.text
    assert nothing_left(await attachment_rows(db), store)


async def test_a_damaged_image_makes_no_derived_files_and_frees_the_task_row(
    editor_client, item, db, store
):
    r = await editor_client.post(
        f"/items/{item.id}/attachments",
        files={"file": ("half.png", samples.png((200, 200))[:60])},
        headers=HX,
    )
    assert r.status_code == 422 and toast_message(r) == BROKEN_IMAGE

    # The transaction that held the item's row has ended: the next upload does not wait for it.
    assert await asyncio.wait_for(upload(editor_client, item.id), 2)
    assert len(await attachment_rows(db)) == 1


@pytest.mark.parametrize(
    "limit", [100, 50], ids=["above the limit (Pillow warns, twice)", "above twice the limit"]
)
async def test_an_image_with_too_many_pixels_is_refused_without_a_warning(
    editor_client, item, db, store, monkeypatch, recwarn, limit
):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", limit)  # the picture has 144 pixels
    filters = list(warnings.filters)

    r = await editor_client.post(
        f"/items/{item.id}/attachments",
        files={"file": ("big.png", samples.png((12, 12)))},
        headers=HX,
    )

    assert r.status_code == 422 and toast_message(r) == BROKEN_IMAGE
    assert nothing_left(await attachment_rows(db), store)
    assert [w.message for w in recwarn if "pixels" in str(w.message)] == []  # none emitted
    assert warnings.filters == filters  # and the process's warning filters are as they were


async def test_an_image_far_over_the_pixel_limit_is_refused(editor_client, item, db, store):
    buffer = io.BytesIO()
    Image.new("1", (14000, 14000)).save(buffer, format="PNG")  # 196 million pixels, 200 KB

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("bomb.png", buffer.getvalue())}, headers=HX
    )

    assert r.status_code == 422 and toast_message(r) == BROKEN_IMAGE
    assert nothing_left(await attachment_rows(db), store)


@pytest.mark.parametrize("heic", [False, True], ids=["thumbnail", "heic preview"])
async def test_a_file_that_cannot_be_written_is_a_server_error_and_leaves_nothing(
    editor_client, item, db, store, monkeypatch, heic
):
    """A full disk is not a broken image: no BROKEN_IMAGE, no row, no file (not the half-written
    thumbnail either)."""
    content, name = (samples.heic(), "IMG.heic") if heic else (samples.png(), "photo.png")

    def full_disk(self, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Image.Image, "save", full_disk)
    editor_client._transport.raise_app_exceptions = False  # the server's 500, not the exception

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": (name, content)}, headers=HX
    )

    assert r.status_code == 500
    assert BROKEN_IMAGE not in r.text and toast_message(r) == SERVER_ERROR  # not a file's fault
    assert nothing_left(await attachment_rows(db), store)


async def test_the_files_dropped_by_a_failing_commit_are_removed(
    editor_client, item, db, store, monkeypatch
):
    async def failing(db, *events):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(attachments, "commit_and_publish", failing)

    with pytest.raises(RuntimeError):
        await editor_client.post(
            f"/items/{item.id}/attachments",
            files={"file": ("photo.png", samples.png())},
            headers=HX,
        )

    assert files_in(store) == []  # the file and its thumbnail
    assert await attachment_rows(db) == []


async def test_a_viewer_cannot_attach_or_delete(viewer_client, editor_client, item, db, store):
    attachment_id = await upload(editor_client, item.id)
    before = files_in(store)

    for files in ({"file": ("note.txt", samples.TEXT)}, None):  # a file, or none: 403 either way
        r = await viewer_client.post(f"/items/{item.id}/attachments", files=files, headers=HX)
        assert r.status_code == 403
        assert toast_message(r) == "You don't have permission to do that."
    r = await viewer_client.delete(f"/attachments/{attachment_id}", headers=HX)
    assert r.status_code == 403

    assert len(await attachment_rows(db)) == 1 and files_in(store) == before


@pytest.mark.parametrize("how", ["wrong token", "no token", "another origin"])
async def test_an_upload_needs_the_csrf_token_and_the_apps_own_origin(
    editor_client, item, db, store, published, how
):
    headers = HX.copy()
    if how == "wrong token":
        headers[CSRF_HEADER] = "not-the-token"
    elif how == "no token":
        del editor_client.headers[CSRF_HEADER]
    else:
        headers["Origin"] = "https://evil.example"

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("note.txt", samples.TEXT)}, headers=headers
    )

    assert r.status_code == 403
    assert nothing_left(await attachment_rows(db), store) and published == []


# ---- limits


async def test_attachment_limits(editor_client, item, db, store):
    for number in range(20):
        await create_attachment(db, item, filename=f"{number}.txt")

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("21.txt", samples.TEXT)}, headers=HX
    )

    assert r.status_code == 409
    assert toast_message(r) == TOO_MANY.format(n=20) == "A task can have at most 20 attachments."
    assert nothing_left([a for a in await attachment_rows(db) if a.filename == "21.txt"], store)

    other = await create_item(db, await db.get_one(Category, item.category_id), "Bread")
    await create_attachment(db, other, size_bytes=2 * 1024**3)  # the instance's quota, used up

    r = await editor_client.post(
        f"/items/{other.id}/attachments", files={"file": ("one-more.txt", samples.TEXT)}, headers=HX
    )

    assert r.status_code == 409
    assert toast_message(r) == STORAGE_FULL == "Attachment storage is full."
    assert files_in(store) == []


async def test_the_last_byte_of_the_quota_and_the_last_slot_of_a_task_are_free(
    editor_client, item, db, store
):
    for number in range(19):
        await create_attachment(db, item, filename=f"{number}.txt")
    other = await create_item(db, await db.get_one(Category, item.category_id), "Bread")
    # 19 files of 14 bytes are in: room for the 20th, which takes the last 14 bytes of the 2 GiB.
    await create_attachment(db, other, size_bytes=2 * 1024**3 - 20 * len(samples.TEXT))

    assert await upload(editor_client, item.id, "20.txt")

    r = await editor_client.post(
        f"/items/{other.id}/attachments", files={"file": ("x.txt", b"x")}, headers=HX
    )
    assert r.status_code == 409 and toast_message(r) == STORAGE_FULL


async def test_deleting_makes_room(editor_client, item, db):
    ids = [await upload(editor_client, item.id, f"{n}.txt") for n in range(3)]
    stored = await db.scalar(select(func.sum(Attachment.size_bytes)))
    assert stored == 3 * len(samples.TEXT)

    await editor_client.delete(f"/attachments/{ids[0]}", headers=HX)

    assert await db.scalar(select(func.sum(Attachment.size_bytes))) == 2 * len(samples.TEXT)


@pytest.mark.parametrize("settings", [{"attachment_max_bytes": MEGABYTE}], indirect=True)
async def test_the_size_limit_is_a_setting(editor_client, item, db, store):
    r = await editor_client.post(
        f"/items/{item.id}/attachments",
        files={"file": ("big.txt", b"x" * (MEGABYTE + 1))},
        headers=HX,
    )

    assert r.status_code == 413 and toast_message(r) == "Files can be at most 1 MB."
    assert await upload(editor_client, item.id, "fits.txt", b"x" * MEGABYTE)
    assert len(files_in(store)) == 1


@pytest.mark.parametrize(
    "settings", [{"attachment_max_bytes": 1_572_864, "attachments_per_item": 2}], indirect=True
)
async def test_the_number_of_files_and_the_fraction_of_a_megabyte_are_settings(
    editor_client, item, db, store
):
    await upload(editor_client, item.id, "one.txt")
    await upload(editor_client, item.id, "two.txt")

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("three.txt", b"x" * 1_572_865)}, headers=HX
    )
    assert r.status_code == 413 and toast_message(r) == "Files can be at most 1.5 MB."

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("three.txt", samples.TEXT)}, headers=HX
    )
    assert r.status_code == 409 and toast_message(r) == "A task can have at most 2 attachments."


@pytest.mark.parametrize("settings", [{"attachments_total_bytes": 20}], indirect=True)
async def test_the_quota_of_the_instance_is_a_setting(editor_client, item, db, store):
    await upload(editor_client, item.id, "one.txt")  # 14 of 20 bytes

    r = await editor_client.post(
        f"/items/{item.id}/attachments", files={"file": ("two.txt", samples.TEXT)}, headers=HX
    )

    assert r.status_code == 409 and toast_message(r) == STORAGE_FULL
    assert len(await attachment_rows(db)) == 1


async def test_two_uploads_at_the_same_moment_respect_the_limit_of_a_task(
    editor_client, item, db, store
):
    for number in range(19):
        await create_attachment(db, item, filename=f"{number}.txt")

    responses = await asyncio.gather(
        *(
            editor_client.post(
                f"/items/{item.id}/attachments",
                files={"file": (f"race-{n}.txt", samples.TEXT)},
                headers=HX,
            )
            for n in range(2)
        )
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(await attachment_rows(db)) == 20
    assert len(files_in(store)) == 1  # the loser wrote nothing


# ---- names


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("receipt.pdf", "receipt.pdf"),
        ("a\x00b\x01c\x7fd\x85e\r\nf.txt", "abcdef.txt"),
        ("  spaced name.txt  ", "spaced name.txt"),
        ("../../etc/passwd.txt", "../../etc/passwd.txt"),  # shown, never used as a path
        ("C:\\fakepath\\photo.png", "C:\\fakepath\\photo.png"),
        ("Łosoś & chips.txt", "Łosoś & chips.txt"),
        ("\x00\x01", "file"),
        ("", "file"),
        # Format characters: the right-to-left override that makes "gpj.exe" read "exe.jpg",
        # zero-width ones, the byte order mark, the soft hyphen.
        ("photo\u202egpj.exe", "photogpj.exe"),
        ("a\u200bb\u200c\u200d\u2060c\ufeff\u00ad.txt", "abc.txt"),
        ("\u202e\u200b", "file"),
        (NASTY + ".txt", NASTY + ".txt"),
        ("x" * 300 + ".txt", "x" * 251 + ".txt"),
        ("x" * 300, "x" * 255),
        ("x" * 300 + "." + "e" * 50, ("x" * 300 + "." + "e" * 50)[:255]),
    ],
)
def test_clean_filename(name, kept):
    cleaned = clean_filename(name)
    assert cleaned == kept
    assert 0 < len(cleaned) <= 255


async def test_the_name_is_kept_for_showing_only(editor_client, item, db, store):
    # Sent as a browser may send it: a path, and control characters (a tab, a bell) in the name.
    body = multipart("name-boundary", "../../etc/a\tb\x07c.txt", samples.TEXT)
    r = await editor_client.post(
        f"/items/{item.id}/attachments",
        content=body,
        headers=HX | {"Content-Type": "multipart/form-data; boundary=name-boundary"},
    )
    assert r.status_code == 200

    (row,) = await attachment_rows(db)

    assert row.filename == "../../etc/abc.txt"
    # Under the key and nowhere else.
    assert files_in(store) == [f"{row.storage_key[:2]}/{row.storage_key}"]
    assert not (store.root.parent / "etc").exists()


# ---- the details and the tile


async def test_details_and_tile_markup(editor_client, viewer_client, item, board, db, store):
    await db.execute(
        update(User).where(User.email == "editor@example.com").values(display_name=NASTY)
    )
    await db.commit()
    uploader = await db.scalar(select(User).where(User.email == "editor@example.com"))
    # (A browser sends a quote in a name as %22, so the name goes in without the form.)
    note = (await create_attachment(db, item, filename=NASTY + ".txt", uploaded_by=uploader)).id
    empty = await create_item(db, await db.get_one(Category, item.category_id), "Bread")

    editor = (await editor_client.get(f"/items/{item.id}/details", headers=HX)).text

    assert f'<li data-attachment-id="{note}"' in editor
    assert 'class="inline-flex min-h-11 items-center gap-2">' in editor
    assert f'href="/attachments/{note}"' in editor
    assert "14 Bytes" in editor
    assert (
        f"{NASTY_ESCAPED}.txt" in editor
    )  # the name, escaped, in the link, the label and the question
    assert f'aria-label="Delete {NASTY_ESCAPED}.txt"' in editor
    assert f'hx-confirm="Delete “{NASTY_ESCAPED}.txt”?"' in editor
    assert f"· {NASTY_ESCAPED} · <time" in editor  # the uploader
    assert re.search(
        r'<time datetime="[^"]+" class="whitespace-nowrap">\d{4}-\d\d-\d\d \d\d:\d\d UTC</time>',
        editor,
    )
    assert NASTY not in editor and "<script>alert(1)</script>" not in editor
    assert f'hx-delete="/attachments/{note}"' in editor
    assert 'class="btn-icon shrink-0 text-slate-600 dark:text-slate-400"' in editor
    assert '<h3 id="item-attachments-title">Attachments</h3>' in editor
    assert 'hx-target="#item-details" hx-swap="outerHTML"' in editor
    assert re.search(
        rf'<form hx-post="/items/{item.id}/attachments" hx-encoding="multipart/form-data"'
        r' hx-disabled-elt="find button"[^>]*>',
        editor,
    )
    assert '<label for="attachment-file" class="label">Add a file</label>' in editor
    assert (
        '<input id="attachment-file" type="file" name="file" required'
        ' accept="image/*,.heic,.heif,application/pdf,.pdf,.txt,text/plain,.md,text/markdown"'
        ' class="block min-h-11 w-full text-sm file:mr-3 file:min-h-11 file:rounded-lg'
        ' file:border-0 file:px-3">'
    ) in editor
    assert "capture" not in editor  # the phone's own picker offers the camera
    assert '<button type="submit" class="btn-secondary">Upload</button>' in editor

    nothing = (await editor_client.get(f"/items/{empty.id}/details", headers=HX)).text
    assert "No attachments yet." in nothing and "data-attachment-id" not in nothing

    # A viewer reads the list and opens the files, and has no control to change it.
    viewer = (await viewer_client.get(f"/items/{item.id}/details", headers=HX)).text
    assert f'href="/attachments/{note}"' in viewer and f"{NASTY_ESCAPED}.txt" in viewer
    assert "14 Bytes" in viewer
    for control in (*EDIT_CONTROLS, 'type="file"', "Add a file", "Upload", "hx-confirm"):
        assert control not in viewer, control
    viewer_nothing = (await viewer_client.get(f"/items/{empty.id}/details", headers=HX)).text
    assert "No attachments yet." in viewer_nothing and "Add a file" not in viewer_nothing

    # The tile: after one file "1 attachment", after two "2 attachments".
    one = '<span class="attachment-count" title="1 attachment"><span aria-hidden="true">📎</span> 1</span>'
    two = '<span class="attachment-count" title="2 attachments"><span aria-hidden="true">📎</span> 2</span>'
    for client in (editor_client, viewer_client):
        tile = (await client.get(f"/categories/{item.category_id}")).text
        assert one in tile and two not in tile
        # The board has two members, so the author shows too; the count comes before the avatar.
    await upload(editor_client, item.id, "second.txt")
    for client in (editor_client, viewer_client):
        tile = (await client.get(f"/categories/{item.category_id}")).text
        assert two in tile and one not in tile
        assert tile.count("attachment-count") == 1  # the item without a file has none


async def test_the_tile_has_a_box_for_a_count_alone(signed_in_client, db, board, item):
    """On a board of one member and an item with no checklist, the file count is all the box holds."""
    owner_client = await signed_in_client("owner@example.com")
    await upload(owner_client, item.id)

    tile = (await owner_client.get(f"/categories/{item.category_id}")).text

    assert re.search(
        r'<div class="shrink-0 py-2\.5 pl-2 \*:align-top">'
        r'<span class="attachment-count" title="1 attachment">'
        r'<span aria-hidden="true">📎</span> 1</span></div>\s*</li>',
        tile,
    )


async def test_a_picture_shows_its_thumbnail_and_a_pdf_opens_in_a_new_tab(editor_client, item, db):
    picture = await upload(editor_client, item.id, "photo.png", samples.png(), "image/png")
    pdf = await upload(editor_client, item.id, "receipt.pdf", samples.PDF, "application/pdf")
    note = await upload(editor_client, item.id, "note.txt")

    html = (await editor_client.get(f"/items/{item.id}/details", headers=HX)).text

    assert f'<img src="/attachments/{picture}/thumb" alt="" width="44" height="44"' in html
    assert 'class="size-11 shrink-0 rounded object-cover"' in html
    assert (
        f'<a href="/attachments/{pdf}/file" target="_blank" rel="noopener" class="inline-flex'
        in html
    )
    assert f'<a href="/attachments/{note}" class="inline-flex' in html
    assert f"/attachments/{note}/thumb" not in html and f"/attachments/{pdf}/thumb" not in html


# ---- lookups and locks


def test_the_lookup_of_load_attachment_is_built_once_for_each_action_and_lock():
    first = _attachment_lookup(Action.EDIT_CONTENT, RowLock.UPDATE)

    assert _attachment_lookup(Action.EDIT_CONTENT, RowLock.UPDATE) is first
    assert _attachment_lookup(Action.EDIT_CONTENT, RowLock.NO_KEY_UPDATE) is not first
    assert _attachment_lookup(Action.VIEW, None) is _attachment_lookup(Action.VIEW, None)


async def test_load_attachment_builds_no_statement(editor_client, item, db):
    attachment_id = await upload(editor_client, item.id)
    editor = await db.scalar(select(User).where(User.email == "editor@example.com"))
    _attachment_lookup.cache_clear()

    for _ in range(3):
        await load_attachment(db, attachment_id, editor, Action.EDIT_CONTENT, lock=RowLock.UPDATE)

    info = _attachment_lookup.cache_info()
    assert (info.misses, info.hits) == (1, 2)


def test_load_attachment_locks_only_the_attachment_for_a_change_and_never_for_a_view():
    view = _attachment_lookup(Action.VIEW, None)
    update_lookup = _attachment_lookup(Action.EDIT_CONTENT, RowLock.UPDATE)
    no_key = _attachment_lookup(Action.EDIT_CONTENT, RowLock.NO_KEY_UPDATE)

    def sql(statement) -> str:
        return str(statement.compile(dialect=postgresql.dialect()))

    assert view.locked is None and "FOR " not in sql(view.query)
    assert "FOR UPDATE OF attachments" in sql(update_lookup.locked)
    assert "FOR NO KEY UPDATE OF attachments" in sql(no_key.locked)
    for lookup in (update_lookup, no_key):
        # The role filter is in the locked statement only; the unlocked one tells 403 from 404.
        assert "board_members.role IN" in sql(lookup.locked)
        assert "board_members.role IN" not in sql(lookup.query)


async def test_a_delete_that_loses_the_race_for_the_row_is_404_with_a_resync(
    editor_client, item, db
):
    attachment_id = await upload(editor_client, item.id)

    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("DELETE FROM attachments WHERE id = :id"), {"id": attachment_id}
        )
        racing = asyncio.create_task(
            editor_client.delete(f"/attachments/{attachment_id}", headers=HX)
        )
        await wait_until_a_query_waits_for_a_lock(db)
        await transaction.commit()
        r = await racing

    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {
        "toast": {"level": "error", "message": ATTACHMENT_GONE},
        "resync": True,
    }


async def test_an_upload_does_not_wait_for_a_lock_that_does_not_conflict(editor_client, item, db):
    """FOR KEY SHARE is what a row that refers to the item (a checklist entry, a file) takes on it."""
    async with db.bind.connect() as other_device, other_device.begin():
        await other_device.execute(
            text("SELECT 1 FROM items WHERE id = :id FOR KEY SHARE"), {"id": item.id}
        )

        assert await asyncio.wait_for(upload(editor_client, item.id), 2)


async def test_an_upload_waits_for_the_lock_of_another_change_to_the_item(editor_client, item, db):
    """The item's row is what serializes the files added at the same time."""
    async with db.bind.connect() as other_device:
        transaction = await other_device.begin()
        await other_device.execute(
            text("SELECT 1 FROM items WHERE id = :id FOR NO KEY UPDATE"), {"id": item.id}
        )
        uploading = asyncio.create_task(upload(editor_client, item.id))
        await wait_until_a_query_waits_for_a_lock(db)
        assert not uploading.done()
        await transaction.commit()

    assert await asyncio.wait_for(uploading, 5)


# ---- the size limit in front of the app


class Reader:
    """An ASGI app that reads the whole body, as the form parser does, and says what became of it.
    With ``answers`` it answers a body cut short with a 400 (what the framework does); without,
    it fails with ``ClientDisconnect`` and answers nothing (the CSRF check, reading a form)."""

    def __init__(self, answers: bool = True) -> None:
        self.answers = answers
        self.called = 0
        self.bytes = 0
        self.end: str | None = None

    async def __call__(self, scope, receive, send):
        self.called += 1
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.end = "disconnected"
                if not self.answers:
                    raise ClientDisconnect
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"error parsing the body"})
                return
            self.bytes += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.end = "complete"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def send_to(reader: Reader, tmp_path, method, path, content, **headers) -> httpx.Response:
    """``content`` sent to ``reader`` behind the size limit (an htmx request, for a toast)."""
    limited = UploadSizeLimitMiddleware(reader, _test_settings(attachments_dir=tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=limited), base_url="http://testserver"
    ) as client:
        return await client.request(method, path, content=content, headers=HX | headers)


async def body_of(size: int):
    """``size`` bytes in 1 MB chunks, with no Content-Length."""
    sent = 0
    while sent < size:
        yield b"x" * min(MEGABYTE, size - sent)
        sent += MEGABYTE


LIMIT = 10 * MEGABYTE + FORM_OVERHEAD


async def test_a_declared_size_over_the_limit_is_refused_without_reading_the_body(tmp_path):
    reader = Reader()

    r = await send_to(reader, tmp_path, "POST", "/items/1/attachments", b"x" * (LIMIT + 1))

    assert r.status_code == 413
    assert toast_message(r) == TOO_LARGE_10
    assert reader.called == 0


async def test_a_body_without_a_declared_size_is_cut_off_at_the_limit_and_answered_413(tmp_path):
    reader = Reader()

    r = await send_to(reader, tmp_path, "POST", "/items/1/attachments", body_of(LIMIT + MEGABYTE))

    assert r.status_code == 413  # not the 400 that the app answered for the body it was cut off in
    assert toast_message(r) == TOO_LARGE_10
    assert reader.end == "disconnected"
    assert reader.bytes <= LIMIT  # the app was never given more than the limit


async def test_an_app_that_fails_with_the_cut_body_and_has_no_answer_gets_one(tmp_path):
    reader = Reader(answers=False)

    r = await send_to(reader, tmp_path, "POST", "/items/1/attachments", body_of(LIMIT + MEGABYTE))

    assert r.status_code == 413 and toast_message(r) == TOO_LARGE_10
    assert reader.end == "disconnected"


async def test_a_body_of_exactly_the_limit_goes_through(tmp_path):
    reader = Reader()

    r = await send_to(reader, tmp_path, "POST", "/items/1/attachments", b"x" * LIMIT)

    assert (r.status_code, r.text) == (200, "ok")
    assert (reader.end, reader.bytes) == ("complete", LIMIT)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/items/1/checklist"),
        ("POST", "/items/x/attachments"),
        ("POST", "/items/1/attachments/2"),
        ("POST", "/other"),
        ("PUT", "/items/1/attachments"),
    ],
)
async def test_the_limit_leaves_other_requests_alone(tmp_path, method, path):
    reader = Reader()

    r = await send_to(reader, tmp_path, method, path, b"x" * (LIMIT + MEGABYTE))

    assert (r.status_code, reader.end) == (200, "complete")


# ---- what a deleted attachment's files do


async def test_delete_attachment_commits_before_it_removes_the_files(db, item, store, monkeypatch):
    owner = await db.scalar(select(User).where(User.email == "owner@example.com"))
    attachment = await create_attachment(db, item, store=store)
    order = []
    commit = attachments.commit_and_publish

    async def spying_commit(db, *events):
        order.append(("commit", store.path(attachment.storage_key).exists()))
        await commit(db, *events)

    monkeypatch.setattr(attachments, "commit_and_publish", spying_commit)

    assert await delete_attachment(db, attachment.id, owner, store) == item.id

    assert order == [("commit", True)]  # the file was still there at the commit
    assert files_in(store) == []


async def test_a_failing_delete_keeps_the_files(db, item, store, monkeypatch):
    owner = await db.scalar(select(User).where(User.email == "owner@example.com"))
    attachment = await create_attachment(db, item, store=store)

    async def failing(db, *events):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(attachments, "commit_and_publish", failing)

    with pytest.raises(RuntimeError):
        await delete_attachment(db, attachment.id, owner, store)

    assert files_in(store) != []
