"""Showing the files: the page for one, the file itself under its own policy and name (Review
Focus 3 and 5), the thumbnail and the preview, Markdown rendered without HTML or script, and the
files of an item that is gone."""

import re
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from content.test_isolation import ROUTES, fill
from factories import create_attachment
from helpers import NASTY, NASTY_ESCAPED, toast_message
from sqlalchemy import update
from test_statement_budget import statements_of

from attachments import samples
from attachments.samples import HX, upload
from attachments.test_upload import files_in
from easytodo.attachments.routes import FILE_CSP, PDF_CSP
from easytodo.attachments.service import ATTACHMENT_GONE
from easytodo.attachments.sniff import download_name
from easytodo.content.service import ITEM_GONE
from easytodo.middleware import CONTENT_SECURITY_POLICY
from easytodo.models import Attachment, Board, Category, Item

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
POLISH_NAME = "Łosoś & chips.txt"
POLISH_DISPOSITION = "filename*=utf-8''%C5%81oso%C5%9B%20%26%20chips.txt"
DELETED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


async def stored(db, item, store, filename="note.txt", content=samples.TEXT, **kwargs):
    """A text (or ``kind``) attachment of ``item``, its file in the store."""
    return await create_attachment(
        db, item, store=store, filename=filename, content=content, **kwargs
    )


def markdown_div(html: str) -> str:
    match = re.search(r'<div class="markdown card">(.*?)</div>\s*\n\s*<p><a href', html, re.DOTALL)
    assert match, "no rendered Markdown on the page"
    return match[1]


# ---- the file


async def test_file_headers(viewer_client, item, db, store):
    attachment = await stored(db, item, store, POLISH_NAME, "Łosoś z frytkami\n".encode())

    r = await viewer_client.get(f"/attachments/{attachment.id}/file")

    assert r.status_code == 200
    assert r.content == "Łosoś z frytkami\n".encode()
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-disposition"] == f"inline; {POLISH_DISPOSITION}"
    assert r.headers["content-security-policy"] == FILE_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "private, no-cache"
    assert r.headers["permissions-policy"] == PERMISSIONS_POLICY  # the camera stays off the page

    r = await viewer_client.get(f"/attachments/{attachment.id}/file?download=1")

    assert r.status_code == 200
    assert r.headers["content-disposition"] == f"attachment; {POLISH_DISPOSITION}"
    assert r.headers["content-security-policy"] == FILE_CSP


LONG_TEXT = b"0123456789" * 500  # 5000 bytes: GZip compresses a response above 1000


def padded_pdf() -> bytes:
    return samples.PDF + b"%" + b"x" * 4000 + b"\n"


BIG_PNG = samples.png() + b"\x00" * 2000  # (only its size matters here)


@pytest.mark.parametrize(
    ("filename", "content", "kind", "content_type", "gzipped"),
    [
        pytest.param("digits.txt", LONG_TEXT, "text", "text/plain", True, id="text"),
        pytest.param("plan.md", LONG_TEXT, "markdown", "text/markdown", True, id="markdown"),
        pytest.param("receipt.pdf", padded_pdf(), "pdf", "application/pdf", False, id="pdf"),
        pytest.param("photo.png", BIG_PNG, "image", "image/png", False, id="png"),
        pytest.param("IMG.heic", b"\x00" * 4000, "image", "image/heic", False, id="heic"),
    ],
)
async def test_text_above_the_minimum_size_is_compressed_and_the_rest_is_not(
    viewer_client, item, db, store, filename, content, kind, content_type, gzipped
):
    assert len(content) > 1000
    attachment = await stored(
        db, item, store, filename, content, kind=kind, content_type=content_type
    )

    r = await viewer_client.get(
        f"/attachments/{attachment.id}/file", headers={"Accept-Encoding": "gzip"}
    )

    assert r.status_code == 200 and r.content == content  # (httpx undoes the compression)
    assert r.headers.get("content-encoding") == ("gzip" if gzipped else None)


async def test_a_range_is_answered_in_parts_and_never_compressed(viewer_client, item, db, store):
    attachment = await stored(db, item, store, "digits.txt", LONG_TEXT)
    url = f"/attachments/{attachment.id}/file"

    # 1500 bytes: above what GZip compresses, so only the 206 keeps it from doing so.
    part = await viewer_client.get(
        url, headers={"Range": "bytes=1500-2999", "Accept-Encoding": "gzip"}
    )

    assert part.status_code == 206
    assert part.content == LONG_TEXT[1500:3000]
    assert part.headers["content-range"] == "bytes 1500-2999/5000"
    assert part.headers["content-length"] == "1500"
    assert "content-encoding" not in part.headers
    assert part.headers["content-security-policy"] == FILE_CSP


@pytest.mark.parametrize(
    ("filename", "content", "kind", "content_type", "path", "csp"),
    [
        pytest.param("note.txt", samples.TEXT, "text", "text/plain", "/file", FILE_CSP, id="text"),
        pytest.param(
            "receipt.pdf", samples.PDF, "pdf", "application/pdf", "/file", PDF_CSP, id="pdf"
        ),
        pytest.param(
            "photo.png", samples.png(), "image", "image/png", "/file?download=1", FILE_CSP, id="png"
        ),
        pytest.param(
            "photo.png", samples.png(), "image", "image/png", "/thumb", FILE_CSP, id="thumb"
        ),
    ],
)
async def test_a_revalidated_file_is_a_304_that_keeps_the_policy_of_the_file(
    viewer_client, item, db, store, filename, content, kind, content_type, path, csp
):
    """A browser replaces the headers it stored with those of the 304: without the file's own
    policy the stored copy would get the page's (the sandbox lost; a PDF's viewer blocked)."""
    attachment = await stored(
        db, item, store, filename, content, kind=kind, content_type=content_type
    )
    store.thumb_path(attachment.storage_key).write_bytes(b"thumb")
    url = f"/attachments/{attachment.id}{path}"

    first = await viewer_client.get(url)
    again = await viewer_client.get(url, headers={"If-None-Match": first.headers["etag"]})
    weak = await viewer_client.get(url, headers={"If-None-Match": f"W/{first.headers['etag']}"})
    other = await viewer_client.get(url, headers={"If-None-Match": '"not-this-one"'})

    assert first.status_code == 200 and first.headers["content-security-policy"] == csp
    assert other.status_code == 200
    for revalidated in (again, weak):
        assert revalidated.status_code == 304
        assert revalidated.content == b""
        assert revalidated.headers["content-security-policy"] == csp
        for name in ("etag", "cache-control", "content-disposition", "last-modified"):
            assert revalidated.headers.get(name) == first.headers.get(name), name
        assert revalidated.headers["cache-control"] == "private, no-cache"
        assert revalidated.headers["x-content-type-options"] == "nosniff"
        assert revalidated.headers["x-frame-options"] == "DENY"
        assert "content-type" not in revalidated.headers


@pytest.mark.parametrize(
    "name",
    [NASTY + ".txt", 'a"; filename="evil.exe', "a\r\nSet-Cookie: x=y.txt", "a;b,c\\d.txt"],
)
async def test_the_name_is_only_ever_sent_percent_encoded(viewer_client, item, db, store, name):
    attachment = await stored(db, item, store, name)  # (a name no upload could have: worse)

    for query, sent in (("", name), ("?download=1", download_name(name, "text/plain"))):
        r = await viewer_client.get(f"/attachments/{attachment.id}/file{query}")

        disposition = r.headers["content-disposition"]
        assert disposition.endswith(f"filename*=utf-8''{quote(sent, safe='')}")
        assert 'filename="' not in disposition and "set-cookie" not in r.headers
        assert re.fullmatch(
            r"(inline|attachment); filename\*=utf-8''[A-Za-z0-9%_.~-]*", disposition
        )


@pytest.mark.parametrize(
    ("filename", "kind", "content_type", "saved_as"),
    [
        # A name whose extension says something else gets the right one added.
        ("invoice.html", "pdf", "application/pdf", "invoice.html.pdf"),
        ("invoice", "pdf", "application/pdf", "invoice.pdf"),
        ("invoice.", "pdf", "application/pdf", "invoice.pdf"),
        ("setup.exe", "text", "text/plain", "setup.exe.txt"),
        ("notes.txt", "markdown", "text/markdown", "notes.txt.md"),
        ("photo.png", "image", "image/jpeg", "photo.png.jpg"),
        ("photo.jpg", "image", "image/png", "photo.jpg.png"),
        ("clip.jpg", "image", "image/gif", "clip.jpg.gif"),
        ("clip.gif", "image", "image/webp", "clip.gif.webp"),
        ("IMG.jpg", "image", "image/heic", "IMG.jpg.heic"),
        # One that fits stays as it is, in any case.
        ("photo.jpg", "image", "image/jpeg", "photo.jpg"),
        ("PHOTO.JPEG", "image", "image/jpeg", "PHOTO.JPEG"),
        ("photo.png", "image", "image/png", "photo.png"),
        ("clip.Gif", "image", "image/gif", "clip.Gif"),
        ("pic.webp", "image", "image/webp", "pic.webp"),
        ("IMG_1.HEIC", "image", "image/heic", "IMG_1.HEIC"),
        ("IMG_1.heif", "image", "image/heic", "IMG_1.heif"),
        ("receipt.PDF", "pdf", "application/pdf", "receipt.PDF"),
        ("note.txt", "text", "text/plain", "note.txt"),
        ("plan.md", "markdown", "text/markdown", "plan.md"),
        ("plan.markdown", "markdown", "text/markdown", "plan.markdown"),
        ("PLAN.MD", "markdown", "text/markdown", "PLAN.MD"),
        ("Łosoś & chips.pdf", "pdf", "application/pdf", "Łosoś & chips.pdf"),
        ("Łosoś & chips", "pdf", "application/pdf", "Łosoś & chips.pdf"),
    ],
)
async def test_a_download_is_saved_with_an_extension_that_fits_what_the_file_is(
    viewer_client, item, db, store, filename, kind, content_type, saved_as
):
    attachment = await stored(
        db, item, store, filename, b"content", kind=kind, content_type=content_type
    )
    url = f"/attachments/{attachment.id}/file"

    download = await viewer_client.get(f"{url}?download=1")
    shown = await viewer_client.get(url)

    assert download.headers["content-disposition"] == (
        f"attachment; filename*=utf-8''{quote(saved_as, safe='')}"
    )
    # Shown in the browser (and in the list) it keeps the name it was given.
    assert shown.headers["content-disposition"] == (
        f"inline; filename*=utf-8''{quote(filename, safe='')}"
    )


async def test_a_name_that_disguises_the_file_is_cleaned_and_a_download_gets_its_real_extension(
    editor_client, item, db, store
):
    """ "annual<RLO>gpj.exe" is shown as "annualexe.jpg": the override is dropped from the name,
    and what is saved ends in what the file is."""
    pdf = await upload(editor_client, item.id, "annual\u202egpj.exe", samples.PDF, "image/jpeg")

    row = await db.get_one(Attachment, pdf)
    download = await editor_client.get(f"/attachments/{pdf}/file?download=1")

    assert row.filename == "annualgpj.exe" and row.kind == "pdf"
    disposition = download.headers["content-disposition"]
    assert disposition == "attachment; filename*=utf-8''annualgpj.exe.pdf"
    assert "%E2%80%AE" not in disposition  # percent-encoded U+202E
    page = await editor_client.get(f"/items/{item.id}/details", headers=HX)
    assert "\u202e" not in page.text and "annualgpj.exe" in page.text


async def test_html_called_txt_is_shown_as_text_under_the_sandboxed_policy(
    viewer_client, item, db, store
):  # Review Focus 5
    page = b"<html><body><script>alert(document.cookie)</script><b>bold</b></body></html>"
    attachment = await stored(db, item, store, "page.txt", page)

    r = await viewer_client.get(f"/attachments/{attachment.id}/file")

    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-security-policy"] == FILE_CSP
    assert "sandbox" in FILE_CSP and "default-src 'none'" in FILE_CSP
    assert r.headers["x-content-type-options"] == "nosniff"

    r = await viewer_client.get(f"/attachments/{attachment.id}")  # the page of the file

    assert r.status_code == 200
    assert "&lt;script&gt;alert(document.cookie)&lt;/script&gt;" in r.text
    assert "<script>alert(document.cookie)" not in r.text and "<b>bold</b>" not in r.text
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY  # the app's own


async def test_a_pdf_is_shown_by_the_browser_under_its_own_policy(viewer_client, item, db, store):
    attachment = await stored(
        db, item, store, "receipt.pdf", samples.PDF, kind="pdf", content_type="application/pdf"
    )

    r = await viewer_client.get(f"/attachments/{attachment.id}/file")

    assert r.status_code == 200 and r.content == samples.PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-security-policy"] == PDF_CSP
    assert "sandbox" not in PDF_CSP and "object-src 'self'" in PDF_CSP
    assert "frame-ancestors 'none'" in PDF_CSP and "default-src 'none'" in PDF_CSP
    assert r.headers["content-disposition"] == "inline; filename*=utf-8''receipt.pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-encoding" not in r.headers  # a PDF is compressed already

    r = await viewer_client.get(f"/attachments/{attachment.id}")  # no page of its own

    assert r.status_code == 303
    assert r.headers["location"] == f"/attachments/{attachment.id}/file"


async def test_an_image_is_served_as_the_type_it_was_found_to_be(
    viewer_client, editor_client, item
):
    png = await upload(editor_client, item.id, "photo.txt", samples.png(), "text/plain")

    r = await viewer_client.get(f"/attachments/{png}/file")

    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-security-policy"] == FILE_CSP
    assert r.content == samples.png()


async def test_other_pages_keep_the_apps_policy(viewer_client, board):
    r = await viewer_client.get(f"/boards/{board.id}")

    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert r.headers["permissions-policy"] == PERMISSIONS_POLICY


# ---- Markdown


MARKDOWN = (
    """# Plan

<script>alert(1)</script>

<img src=x onerror=alert(1)>

[a](javascript:alert(1)) [b](JaVaScRiPt:alert(1)) <javascript:alert(1)>
[c](&#x6A;avascript:alert(1)) [d](data:text/html,<script>alert(1)</script>)
[e](vbscript:msgbox(1)) [f](<javascript:alert(1)>)

![p](https://tracker.example/p.png)

[ok](https://example.com) [m](mailto:a@example.com) [r](/boards)

<b>raw</b> and **bold** and `code`

"""
    + NASTY
    + "\n"
)


async def test_markdown_is_rendered_without_html_or_javascript_links(
    viewer_client, item, db, store
):  # Review Focus 3
    attachment = await stored(
        db, item, store, "plan.md", MARKDOWN.encode(), kind="markdown", content_type="text/markdown"
    )

    r = await viewer_client.get(f"/attachments/{attachment.id}")

    assert r.status_code == 200
    part = markdown_div(r.text)
    assert "<h1>Plan</h1>" in part
    assert "<strong>bold</strong>" in part and "<code>code</code>" in part
    assert all(tag not in part for tag in ("<script", "<img", "<b>", 'onerror="', "<javascript"))
    # (Markdown escapes text in its own way: `&quot;` for a quote, the apostrophe as it is.)
    assert "O'Reilly &lt;b&gt;&quot;x&quot;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;" in part
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in part and "&lt;b&gt;raw&lt;/b&gt;" in part
    # A picture that is not shown is a plain link; the links that run script are no links.
    assert re.findall(r'href="([^"]*)"', part) == [
        "https://tracker.example/p.png",
        "https://example.com",
        "mailto:a@example.com",
        "/boards",
    ]
    assert not re.search(r"""href="\s*(javascript|data|vbscript)""", part, re.IGNORECASE)
    assert "<img" not in r.text.split('class="markdown card"', 1)[1]
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def scheme_a_browser_reads(href: str) -> str | None:
    """The scheme of ``href`` as a browser takes it: tabs and line breaks inside are dropped, the
    control characters and spaces before it too, and the case does not matter."""
    href = re.sub(r"[\t\r\n]", "", href).lstrip("".join(map(chr, range(0x21))))
    found = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*):", href)
    return found[1].lower() if found else None


@pytest.mark.parametrize(
    "link",
    [
        "[x](javascript:alert(1))",
        "[x](JAVASCRIPT:alert(1))",
        "[x](<javascript:alert(1)>)",
        "[x](<  javascript:alert(1)>)",
        "[x](java&#9;script:alert(1))",
        "[x](java&#10;script:alert(1))",
        "[x](&#x6A;avascript:alert(1))",
        "[x](&#0001;javascript:alert(1))",
        "[x](jav&#x09;ascript:alert(1))",
        "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        "[x](vbscript:msgbox(1))",
        "[x](file:///etc/passwd)",
        "[x]: javascript:alert(1)\n\n[x]",
        "<javascript:alert(1)>",
        "<data:text/html,x>",
        "![x](javascript:alert(1))",
    ],
)
async def test_no_link_in_markdown_runs_script(
    viewer_client, item, db, store, link
):  # Review Focus 3
    attachment = await stored(
        db, item, store, "link.md", link.encode(), kind="markdown", content_type="text/markdown"
    )

    r = await viewer_client.get(f"/attachments/{attachment.id}")

    part = markdown_div(r.text)
    for href in re.findall(r'href="([^"]*)"', part):
        assert scheme_a_browser_reads(href) in {None, "http", "https", "mailto"}, href
    assert "<script" not in part and "<img" not in part


async def test_markdown_is_served_as_plain_text_too(viewer_client, item, db, store):
    attachment = await stored(
        db, item, store, "plan.md", b"# Plan\n<script>x</script>\n", kind="markdown",
        content_type="text/markdown",
    )  # fmt: skip

    r = await viewer_client.get(f"/attachments/{attachment.id}/file")

    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-security-policy"] == FILE_CSP
    assert r.content == b"# Plan\n<script>x</script>\n"


async def test_a_text_with_a_byte_order_mark_or_bad_bytes_is_still_shown(
    viewer_client, item, db, store
):
    with_mark = await stored(db, item, store, "bom.txt", b"\xef\xbb\xbfhello\n")
    broken = await stored(db, item, store, "broken.txt", b"caf\xe9\n")

    r = await viewer_client.get(f"/attachments/{with_mark.id}")
    assert r.status_code == 200 and "﻿" not in r.text and "hello" in r.text

    r = await viewer_client.get(f"/attachments/{broken.id}")
    assert r.status_code == 200 and "caf�" in r.text


async def test_a_text_that_starts_with_a_line_break_keeps_it(viewer_client, item, db, store):
    attachment = await stored(db, item, store, "lines.txt", b"\nsecond line\n")

    r = await viewer_client.get(f"/attachments/{attachment.id}")

    assert re.search(r"<pre[^>]*>\n\nsecond line\n</pre>", r.text)


# ---- the page, the thumbnail and the preview


async def test_view_thumb_and_preview(viewer_client, editor_client, item, db, store):
    picture = await upload(editor_client, item.id, "photo.png", samples.png(), "image/png")
    heic = await upload(editor_client, item.id, "IMG_1.heic", samples.heic(), "image/heic")
    pdf = await upload(editor_client, item.id, "receipt.pdf", samples.PDF, "application/pdf")
    note = await upload(editor_client, item.id, "note.txt")
    plan = await upload(editor_client, item.id, "plan.md", b"# Plan\n", "text/markdown")
    nasty = await stored(db, item, store, NASTY + ".txt")

    # The page of each file.
    for attachment_id, expected in [
        (picture, f'<img src="/attachments/{picture}/file"'),
        (heic, f'<img src="/attachments/{heic}/preview"'),
        (note, "Buy oat milk."),
        (plan, "<h1>Plan</h1>"),
    ]:
        r = await viewer_client.get(f"/attachments/{attachment_id}")
        assert r.status_code == 200, attachment_id
        assert expected in r.text
        assert f"/attachments/{attachment_id}/file?download=1" in r.text
        assert f'href="/boards/{item.board_id}"' in r.text and "Back to the board" in r.text
    r = await viewer_client.get(f"/attachments/{pdf}")
    assert r.status_code == 303

    # The files themselves.
    for attachment_id in (picture, heic, pdf, note, plan):
        r = await viewer_client.get(f"/attachments/{attachment_id}/file")
        assert r.status_code == 200, attachment_id

    # Thumbnails: pictures only. Previews: a HEIC photo only.
    for attachment_id, thumb, preview in [
        (picture, 200, 404),
        (heic, 200, 200),
        (pdf, 404, 404),
        (note, 404, 404),
        (plan, 404, 404),
    ]:
        r = await viewer_client.get(f"/attachments/{attachment_id}/thumb")
        assert r.status_code == thumb, (attachment_id, "thumb")
        if thumb == 200:
            assert r.headers["content-type"] == "image/webp"
            assert r.headers["content-security-policy"] == FILE_CSP
            assert "content-disposition" not in r.headers
        r = await viewer_client.get(f"/attachments/{attachment_id}/preview")
        assert r.status_code == preview, (attachment_id, "preview")
        if preview == 200:
            assert r.headers["content-type"] == "image/jpeg"
            assert r.headers["content-security-policy"] == FILE_CSP

    # A name with markup is text on the page: in the title, the heading and the picture's alt.
    r = await viewer_client.get(f"/attachments/{nasty.id}")
    assert f"<title>{NASTY_ESCAPED}.txt · EasyToDo</title>" in r.text
    assert f'<h1 class="break-words text-2xl font-semibold">{NASTY_ESCAPED}.txt</h1>' in r.text
    assert NASTY not in r.text and "<script>alert(1)</script>" not in r.text
    r = await viewer_client.get(f"/attachments/{nasty.id}/file")
    assert (
        r.headers["content-disposition"]
        == f"inline; filename*=utf-8''{quote(NASTY + '.txt', safe='')}"
    )


async def test_a_pictures_alt_text_is_its_escaped_name(viewer_client, item, db, store):
    attachment = await stored(
        db, item, store, NASTY + ".png", samples.png(), kind="image", content_type="image/png"
    )

    r = await viewer_client.get(f"/attachments/{attachment.id}")

    assert f'alt="{NASTY_ESCAPED}.png"' in r.text
    assert NASTY not in r.text


# ---- a file that is not there, and an item that is gone


async def test_a_row_without_its_file_is_404_and_changes_nothing(viewer_client, item, db, store):
    attachment = await create_attachment(db, item)  # no file in the store

    for path in ("", "/file", "/thumb", "/preview"):
        r = await viewer_client.get(f"/attachments/{attachment.id}{path}")
        assert r.status_code == 404, path
        assert ATTACHMENT_GONE in r.text

    r = await viewer_client.get(f"/attachments/{attachment.id}/file", headers=HX)
    assert r.status_code == 404 and toast_message(r) == ATTACHMENT_GONE


async def test_the_page_of_a_picture_whose_file_is_lost_is_404(viewer_client, item, db, store):
    attachment = await create_attachment(
        db, item, kind="image", content_type="image/png", filename="lost.png"
    )

    assert (await viewer_client.get(f"/attachments/{attachment.id}")).status_code == 404


async def put_out_of_reach(db, how: str, item: Item) -> None:
    category = await db.get_one(Category, item.category_id)
    if how == "deleted item":
        statement = update(Item).where(Item.id == item.id).values(deleted_at=DELETED_AT)
    elif how == "archived category":
        statement = (
            update(Category).where(Category.id == category.id).values(archived_at=DELETED_AT)
        )
    else:
        statement = (
            update(Board).where(Board.id == category.board_id).values(archived_at=DELETED_AT)
        )
    await db.execute(statement)
    await db.commit()


@pytest.mark.parametrize("gone", ["deleted item", "archived category", "archived board"])
async def test_attachment_routes_are_404_once_its_item_is_gone(
    editor_client, item, category, board, db, store, content_snapshot, gone
):  # Review Focus 1
    attachment = await stored(
        db, item, store, "photo.png", samples.png(), kind="image", content_type="image/png"
    )
    store.thumb_path(attachment.storage_key).write_bytes(b"thumb")
    await put_out_of_reach(db, gone, item)
    before, files = await content_snapshot(), files_in(store)
    ids = {
        "board": board.id,
        "category": category.id,
        "item": item.id,
        "attachment": attachment.id,
    }

    seen = 0
    for route in ROUTES:
        if "attachment" not in route[1]:
            continue
        method, path, _ = fill(route, **ids)
        files_to_send = {"file": ("late.txt", samples.TEXT)} if method == "POST" else None
        r = await editor_client.request(method, path, files=files_to_send, headers=HX)
        assert r.status_code == 404, (method, path)
        assert toast_message(r) in {ATTACHMENT_GONE, ITEM_GONE}
        seen += 1

    assert seen == 6
    assert await content_snapshot() == before
    assert files_in(store) == files


# ---- statements


async def test_attachment_statement_budget(app, editor_client, item, db, store):
    """Like the checklist's (8 to add, 7 to tick): the session, the item (or the file) with its
    lock, the file count and the bytes in one statement, the change, its event and the details'
    three. Showing a file is the session and one lookup, whatever the file."""
    attachment = await create_attachment(db, item, store=store)

    with statements_of(app) as uploaded:
        r = await editor_client.post(
            f"/items/{item.id}/attachments",
            files={"file": ("new.txt", samples.TEXT)},
            headers=HX,
        )
    assert r.status_code == 200
    shown = {}
    for name, path in [
        ("page", ""),
        ("file", "/file"),
        ("download", "/file?download=1"),
    ]:
        with statements_of(app) as shown[name]:
            r = await editor_client.get(f"/attachments/{attachment.id}{path}")
        assert r.status_code == 200
    with statements_of(app) as deleted:
        r = await editor_client.delete(f"/attachments/{attachment.id}", headers=HX)
    assert r.status_code == 200

    assert len(uploaded) <= 8, uploaded
    assert len(deleted) <= 7, deleted
    assert {name: len(found) for name, found in shown.items()} == {
        "page": 2,
        "file": 2,
        "download": 2,
    }
    assert sum("sum(attachments.size_bytes)" in statement for statement in uploaded) == 1
