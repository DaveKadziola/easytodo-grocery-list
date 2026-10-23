"""What a file is, taken from its first bytes: the extension and the name the browser sent are only
a hint for a file without a signature (text)."""

import pytest

from attachments import samples
from easytodo.attachments.sniff import Detected, detect

IMAGE = "image"


def heic_head(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + b"mif1heic"


@pytest.mark.parametrize(
    ("head", "filename", "expected"),
    [
        (samples.jpeg()[:32], "photo.jpg", Detected(IMAGE, "image/jpeg")),
        (samples.png()[:32], "photo.png", Detected(IMAGE, "image/png")),
        (samples.gif()[:32], "party.gif", Detected(IMAGE, "image/gif")),
        (samples.webp()[:32], "photo.webp", Detected(IMAGE, "image/webp")),
        (samples.heic()[:32], "IMG_0001.HEIC", Detected(IMAGE, "image/heic")),
        (samples.PDF[:32], "receipt.pdf", Detected("pdf", "application/pdf")),
        # The signature decides, whatever the name says.
        (samples.png()[:32], "notes.txt", Detected(IMAGE, "image/png")),
        (samples.PDF[:32], "photo.png", Detected("pdf", "application/pdf")),
        (samples.jpeg()[:32], "no extension", Detected(IMAGE, "image/jpeg")),
        # No signature: only a text extension makes it text.
        (b"Buy oat milk.\n", "list.txt", Detected("text", "text/plain")),
        (b"# Plan\n", "plan.md", Detected("markdown", "text/markdown")),
        (b"# Plan\n", "plan.markdown", Detected("markdown", "text/markdown")),
        (b"# Plan\n", "PLAN.MD", Detected("markdown", "text/markdown")),
        (b"", "empty.txt", Detected("text", "text/plain")),
        (b"Buy oat milk.\n", "archive.tar.txt", Detected("text", "text/plain")),
        (b"Buy oat milk.\n", "list.txt.exe", None),
        (b"<html><script>alert(1)</script>", "page.html", None),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "logo.svg", None),
        (b"Buy oat milk.\n", "list", None),
        (b"Buy oat milk.\n", "", None),
        # A program (or anything else) with a picture's name.
        (samples.WINDOWS_PROGRAM, "photo.png", None),
        (samples.WINDOWS_PROGRAM, "logo.svg", None),
        (samples.WINDOWS_PROGRAM, "setup.exe", None),
        (samples.WINDOWS_PROGRAM, "list.txt", Detected("text", "text/plain")),  # checked later
        # Heads that only start like a known type.
        (b"\xff\xd8", "photo.jpg", None),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "sound.webp", None),
        (b"GIF90a", "party.gif", None),
        (b"%PDF", "receipt.pdf", None),
        (b" %PDF-1.4", "receipt.pdf", None),
        (b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00", "photo.avif", None),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00", "clip.mp4", None),
        (b"", "photo.png", None),
    ],
)
def test_detect(head, filename, expected):
    assert detect(head, filename) == expected


@pytest.mark.parametrize("brand", [b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"])
def test_every_heic_brand_is_a_picture(brand):
    assert detect(heic_head(brand), "IMG.heic") == Detected(IMAGE, "image/heic")
    assert detect(heic_head(brand), "IMG.txt") == Detected(IMAGE, "image/heic")


def test_a_longer_head_than_needed_is_fine():
    assert detect(samples.png(), "photo.png") == Detected(IMAGE, "image/png")
    assert detect(samples.PDF, "receipt.pdf") == Detected("pdf", "application/pdf")
