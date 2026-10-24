"""Generated sample files for the attachment tests (nothing is checked in), and ``upload``, which
attaches one over HTTP."""

import io
import re

import httpx
import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

HX = {"HX-Request": "true"}
TEXT = b"Buy oat milk.\n"
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
WINDOWS_PROGRAM = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 56


def image(fmt: str, size: tuple[int, int] = (32, 24), **options) -> bytes:
    """A picture of ``size`` in this format ("PNG", "JPEG", "WEBP", "GIF", "HEIF")."""
    picture = Image.new("RGB", size, (200, 30, 30))
    picture.paste((30, 30, 200), (0, 0, size[0] // 2, size[1] // 2))  # a corner that is not red
    out = io.BytesIO()
    picture.save(out, format=fmt, **options)
    return out.getvalue()


def png(size: tuple[int, int] = (32, 24)) -> bytes:
    return image("PNG", size)


def jpeg(size: tuple[int, int] = (32, 24)) -> bytes:
    return image("JPEG", size)


def webp(size: tuple[int, int] = (32, 24)) -> bytes:
    return image("WEBP", size)


def gif(size: tuple[int, int] = (32, 24)) -> bytes:
    return image("GIF", size)


def heic(size: tuple[int, int] = (64, 48)) -> bytes:
    return image("HEIF", size)


def jpeg_rotated_by_exif(size: tuple[int, int] = (40, 20)) -> bytes:
    """A JPEG whose EXIF orientation (6) says to turn it a quarter: shown, it is ``size``
    swapped."""
    exif = Image.Exif()
    exif[0x0112] = 6
    return image("JPEG", size, exif=exif)


def pixels_of(content: bytes) -> tuple[str, tuple[int, int]]:
    """The format and size of a picture, as Pillow reads them."""
    with Image.open(io.BytesIO(content)) as picture:
        return picture.format, picture.size


def multipart(boundary: str, filename: str, content: bytes) -> bytes:
    """The body of a form with one file field "file"."""
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    )
    return head.encode() + content + f"\r\n--{boundary}--\r\n".encode()


async def upload(
    client: httpx.AsyncClient,
    item_id: int,
    filename: str = "note.txt",
    content: bytes = TEXT,
    content_type: str = "text/plain",
) -> int:
    """Attach the file to the item as ``client``'s user would (a multipart form with the field
    "file", from an htmx page); returns the new attachment's id. The file goes to the app's store."""
    r = await client.post(
        f"/items/{item_id}/attachments",
        files={"file": (filename, content, content_type)},
        headers=HX,
    )
    assert r.status_code == 200, (r.status_code, r.headers.get("hx-trigger"))
    return max(int(found) for found in re.findall(r'data-attachment-id="(\d+)"', r.text))
