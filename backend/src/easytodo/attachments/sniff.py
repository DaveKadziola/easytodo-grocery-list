"""What an uploaded file is, taken from its first bytes.

Browsers send a file name and a content type of the user's choosing, so neither is believed: a
file is an image or a PDF when it starts like one, whatever it is called. Only a file with no
signature at all, text, goes by its extension (the service then checks that it is UTF-8). Anything
else, SVG and HTML included, is refused: it could carry a script.
"""

from dataclasses import dataclass

# Read this many bytes of a file to ``detect`` it.
HEAD_SIZE = 16
HEIC = "image/heic"  # a photo from an iPhone: browsers other than Safari cannot show it

# The "major brand" of an ISO base media file (bytes 8-12, after "ftyp"): what iPhones write for a
# photo (heic), and the other HEIF brands that hold pictures. Not "avif" or "isom" (video).
HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"})
TEXT_EXTENSIONS = {".txt": "text", ".md": "markdown", ".markdown": "markdown"}
TEXT_TYPES = {"text": "text/plain", "markdown": "text/markdown"}


@dataclass(frozen=True)
class Detected:
    kind: str  # one of models.ATTACHMENT_KINDS
    content_type: str


def _signature(head: bytes) -> Detected | None:
    if head.startswith(b"\xff\xd8\xff"):
        return Detected("image", "image/jpeg")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Detected("image", "image/png")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return Detected("image", "image/gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Detected("image", "image/webp")
    if head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
        return Detected("image", HEIC)
    if head.startswith(b"%PDF-"):
        return Detected("pdf", "application/pdf")
    return None


def _extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def detect(head: bytes, filename: str) -> Detected | None:
    """The type of a file that starts with ``head`` (at least ``HEAD_SIZE`` bytes of it, or all
    of it if it is shorter), or None if it may not be attached."""
    found = _signature(head)
    if found is not None:
        return found
    kind = TEXT_EXTENSIONS.get(_extension(filename))
    return None if kind is None else Detected(kind, TEXT_TYPES[kind])
